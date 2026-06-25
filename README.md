# AWANA - account specific AWS what's new announcements

> ⚠️ **Disclaimer:** This is demo/prototype code intended for learning and experimentation. It is not production-ready and should not be deployed to production environments without thorough review, hardening, and testing. Use at your own risk.

## What It Does

AWANA (AWS What's New Analyzer by AgenCore) crawls the [AWS What's New](https://aws.amazon.com/new/) RSS feed every weekday morning and decides which announcements matter to you based on your actual AWS usage — services you run, instance types you pay for, database engines in your accounts, and preferences you've stated.

It does this by running each announcement through a two-stage evaluation pipeline (built on [Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/) with [Strands Agents](https://strandsagents.com/)). The first stage is an account-agnostic classification pass that short-circuits when possible — catching universally relevant announcements (preferences, new services, billing/IAM) or filtering out announcements that don't match the org's inventory at all. Announcements that survive are routed to a per-account-group evaluation stage where a single merged evaluator agent combines preference matching, EC2/RDS/ElastiCache specialist logic, and service usage correlation into one decision per account group.

Results are surfaced through a CloudFront-hosted frontend protected by Cognito authentication.

## Prerequisites

- Node.js 22+
- AWS CDK v2 (`npm install -g aws-cdk`)
- An AWS account with sufficient permissions
- AWS Cost Explorer enabled — must be activated in the [Billing console](https://console.aws.amazon.com/cost-management/home#/cost-explorer) before deploying. It takes up to 24 hours to become active. The billing dimensions Lambda will return empty results until Cost Explorer is ready.
- AWS Resource Explorer enabled at organization level to capture all resources
- If using multi-account setup: must be deployed to the **AWS Organizations management account** or a **delegated administrator account** — the account registration API validates accounts against the Organization via `organizations:DescribeAccount`, which is only available from these accounts

## How the Evaluation Works

### Ingestion

A Step Function runs on weekdays at 07:00 UTC:

1. **Resource Inventory** — queries AWS Resource Explorer across all regions and writes a snapshot to S3. That view should cover all resources.
2. **Billing Dimensions** — pulls EC2 instance types, database engines, cache engines, platforms, and regions from Cost Explorer.
3. **Consolidation** — merges inventory + billing into a single `consolidated-context.json`, grouping accounts with identical usage fingerprints to avoid redundant evaluations.
4. **Content Crawler** — a Python Lambda that fetches the RSS feed, deduplicates against a DynamoDB watermark + dedup table, and queues new announcements to SQS.

### Evaluation Pipeline

Each announcement goes through a two-stage pipeline:

**Stage 1: Account-Agnostic Classification** (`classify` mode)

Runs three checks sequentially, short-circuiting when a conclusive result is reached:

1. **Account-Agnostic Classifier** — combines preference matching and broad category filtering (new services, billing, IAM, security) in a single LLM call. If the announcement matches a global preference or a universally-relevant category → `relevant_all` (skip per-account evaluation). If it's clearly not relevant to any preference or category → `not_relevant_all`.
2. **Pre-Filter** — extracts services, database engines, cache engines, EC2 platforms, and regions from the announcement and matches them against the org-wide inventory. If nothing matches → `not_relevant_all`. Special handling for region-expansion announcements (only passes if the org operates in the announced region).
3. **Service Router** — identifies the primary AWS service and classifies the announcement as `single_service` (deterministic per-account matching is sufficient) or `multi_service` (needs deeper specialist evaluation per account group).

**Stage 2: Per-Account-Group Evaluation** (`evaluate` mode)

For announcements that pass Stage 1, a single **per-account-evaluator** agent runs once per account group. This merged agent combines all specialist logic into one LLM call:

- Preference matching (account-specific + global preferences)
- EC2 specialist (instance family/size/platform matching)
- RDS/database specialist (engine matching from billing data)
- ElastiCache/caching specialist (cache engine matching from billing data)
- Service usage correlation (fallback — cross-references the resource inventory)

The evaluator uses the account group's specific inventory, billing dimensions, and preferences as context. It must conclude with "relevant" or "not relevant" — there is no "not my scope" escape.

### Feedback Loop

Users rate announcements as thumbs-up or thumbs-down in the frontend:

1. Feedback is written to the preferences DynamoDB table.
2. A DynamoDB Stream triggers the **Feedback Analyst Lambda**.
3. The analyst uses a Bedrock-backed Strands agent to narrate the rating as a multi-dimensional preference signal (service, feature category, use case).
4. The narration is stored in **AgentCore Memory** via a `UserPreferenceMemoryStrategy`.
5. On the next evaluation run, each evaluator agent retrieves learned preferences from memory alongside explicit preferences and usage data.

## Stacks

| Stack | Purpose |
|---|---|
| `AWANAFoundationStack` | DynamoDB tables, S3 inventory bucket, S3 prompts bucket, SNS alerts, permissions boundary |
| `AWANAIngestionStack` | Step Function (inventory, billing, consolidation, crawler), SQS queue, DLQ |
| `AWANAEvaluationStack` | AWANA agent on AgentCore, evaluation state machine, SQS trigger |
| `AWANAFrontendStack` | S3 website, CloudFront, Lambda@Edge auth, Cognito, API Gateway |
| `edge-lambda-stack-*` | Auto-created by CDK for the Lambda@Edge function (deployed to us-east-1) |

## Deployment
Use below script and follow instructions

```bash
./scripts/project-setup.sh
```

The setup script will automatically create a local `config.ts` from `config.example.ts` on first run. Edit `config.ts` to customize settings (inference profile, log retention, allowed email domains, etc.) before deploying.

If deploying manually without the setup script, pass the required context keys on the first run (see [Configuration](#configuration)):

```bash
npx cdk deploy --all \
  -c region=eu-west-1 \
  -c prefix=AWANA \
  -c resourceExplorerViewArn=arn:aws:resource-explorer-2:eu-west-1:123456789012:view/my-view/abcd1234-...
```

After initial setup, subsequent deploys just need:

```bash
npx cdk deploy --all
```

## Deletion

Deleting AWANA requires a two-phase process because Lambda@Edge replicas are distributed to CloudFront edge locations worldwide and can take few hours to drain after disassociation.

```bash
# Phase 1: disable CloudFront, remove Lambda@Edge, delete all stacks except edge-lambda
./scripts/delete-app.sh

# Wait 30-60 minutes for Lambda@Edge replicas to drain and try again

# Phase 2: delete the edge-lambda stack
./scripts/delete-app.sh
```

The script handles everything automatically:

1. **Disables the CloudFront distribution** 
2. **Removes Lambda@Edge associations** in a single atomic update. Note that without that association your landing page would be public what is why we disable distribution first.
2. **Verifies** the distribution is truly disabled before proceeding (security check).
3. **Waits** for the distribution to reach "Deployed" status.
4. **Deletes all CDK stacks** in reverse dependency order (Frontend → Evaluation → Ingestion → Foundation), skipping the edge-lambda stack.
5. **Saves state** to a `.delete-app-state` file so the second run knows to pick up where it left off.

On the second run, it deletes the remaining `edge-lambda-stack-*` in us-east-1 and the dependent `FoundationStack`. If you prefer to do this manually:

```bash
aws cloudformation delete-stack --stack-name <edge-lambda-stack-name> --region us-east-1
aws cloudformation delete-stack --stack-name <FoundationStackName> --region <region>
```

> **Note:** If you run Phase 2 too early, the delete will fail with "Cannot delete function — replicas still exist". Just wait a bit longer and re-run.

## Configuration

Deployment-specific values (region, prefix, Resource Explorer view ARN) are passed as CDK context parameters and stored in `cdk.context.json` (gitignored) — they are **not** committed to the repo.

The `project-setup.sh` script handles this automatically. After initial setup, subsequent deploys need no extra flags:

```bash
npx cdk deploy --all
```

If deploying manually without the setup script, pass the context keys on the first run:

```bash
npx cdk deploy --all \
  -c region=eu-west-1 \
  -c prefix=AWANA \
  -c resourceExplorerViewArn=arn:aws:resource-explorer-2:eu-west-1:123456789012:view/my-view/abcd...
```

| Context Key | Required | Description |
|---|---|---|
| `region` | Yes | AWS region to deploy into |
| `prefix` | Yes | Resource name prefix (enables multiple deployments per account) |
| `resourceExplorerViewArn` | Yes | ARN of the Resource Explorer view for inventory discovery |

These are persisted to `cdk.context.json` by the setup script so that `cdk deploy --all` and `delete-app.sh` work without arguments on subsequent runs.

Other configuration lives in `config.ts` (created from `config.example.ts` by the setup script — not committed to git):

| Setting | Description |
|---|---|
| `GlobalConfig.logRetentionDays` | CloudWatch log retention (default: 30) |
| `GlobalConfig.fanOutMaxConcurrency` | Max concurrency for the Step Functions Map state fan-out (default: 5) |
| `GlobalConfig.inferenceProfileId` | Bedrock cross-region inference profile used by all agents |
| `AgentConfig.runtimeName` | AgentCore runtime name (derived from prefix) |
| `FrontendConfig.allowedEmailDomains` | Cognito self-registration domains |
| `ApiGatewayConfig.throttlingRateLimit` | Steady-state request rate limit in requests/second (default: 50) |
| `ApiGatewayConfig.throttlingBurstLimit` | Maximum burst capacity before throttling (default: 100) |
| `LambdaConfig.logLevel` | Log level for all Lambda functions (default: 'ERROR') |

Agent prompts and model IDs are in `lib/agents/prompts.ts`, deployed to an S3 prompts bucket at `config/prompts.json`. Prompt changes only need a stack update, not a container redeploy.

## Region Filtering

The billing dimensions Lambda determines which AWS regions are "in use" by querying Cost Explorer for actual spend grouped by region. Only regions with more than **$1/month** in blended cost are included in the org-wide region list used by the pre-filter agent.

This threshold exists because org-wide security services (GuardDuty, CloudTrail, Config, KMS default keys) run in every enabled region and generate small charges ($0.01–$0.20/month) that would otherwise cause every region to appear "active". Without the threshold, region-expansion announcements for services like Glue or Lambda would incorrectly be marked as relevant for regions where only security baselines exist.

**Corner cases:** If you have legitimate low-spend workloads in a region (e.g. a dev/test environment under $1/month), the pre-filter may exclude announcements for that region. To work around this, add a preference statement in the preferences table such as:
- *"I use a Lambda function in ap-south-1 for development and testing"*
- *"All analytics updates to region ap-southeast-3 are relevant to me"*

The customer-preference-matcher logic (now part of the account-agnostic classifier) runs before the pre-filter and will catch these.

## Resilience

- **Deduplication** — DynamoDB date watermark + dedup table (30-day TTL) prevents reprocessing.
- **DLQ self-healing** — failed messages go to a DLQ; the handler removes the dedup entry and rewinds the watermark so items retry on the next crawl.
- **Circuit breaker** — persistent failures (deleted agent, revoked permissions, service outage) disable the SQS event source and alert via SNS.
- **CloudWatch alarms** — every Lambda has an error-rate alarm (≥1 error / 5 min) fanning out to the shared alerts topic.

## Monitoring & Observability

### CloudWatch Alarms

Every Lambda function has an error-rate alarm (≥1 error in 5 minutes) and a throttle alarm that publish to the shared SNS alerts topic. The agent stack adds one additional alarm:

| Alarm | Trigger | Description |
|---|---|---|
| `AgentRuntimeErrorAlarm` | ≥1 ERROR/Traceback/Exception log line in 5 min | Agent runtime is logging errors — model invocations or pipeline logic may be failing |

The evaluation Step Function also has an EventBridge rule that catches `FAILED`, `TIMED_OUT`, and `ABORTED` executions and routes them to the alerts topic.

### Token Usage Logging

The AWANA agent emits structured `TOKEN_METRICS` log lines after every model invocation, capturing input/output/total token counts, cache metrics, and execution duration. These can be queried via CloudWatch Logs Insights.

**CloudWatch Logs Insights queries** (log group: `/aws/bedrock-agentcore/runtimes/<runtimeId>-DEFAULT`):

```
# Total tokens consumed in the last 24 hours, grouped by agent
filter @message like /TOKEN_METRICS/
| parse @message '"agent_name": "*"' as agent_name
| parse @message '"total_tokens": *,' as total_tokens
| stats sum(total_tokens) as tokens by agent_name

# Average tokens per invocation by mode
filter @message like /TOKEN_METRICS/
| parse @message '"mode": "*"' as mode
| parse @message '"input_tokens": *,' as input_tokens
| parse @message '"output_tokens": *,' as output_tokens
| stats avg(input_tokens) as avg_in, avg(output_tokens) as avg_out, count(*) as invocations by mode

# Cache hit ratio (when prompt caching is active)
filter @message like /TOKEN_METRICS/
| parse @message '"cache_read_tokens": *,' as cache_read
| parse @message '"input_tokens": *,' as input_tokens
| stats sum(cache_read) as cached, sum(input_tokens) as total
| display cached, total, (cached / total * 100) as cache_hit_pct

# Duration and cycle count per agent
filter @message like /TOKEN_METRICS/
| parse @message '"agent_name": "*"' as agent_name
| parse @message '"duration_seconds": *,' as duration
| parse @message '"cycles": *,' as cycles
| stats avg(duration) as avg_duration, max(duration) as max_duration, avg(cycles) as avg_cycles by agent_name
```

### Circuit Breaker

When the agent encounters persistent infrastructure failures (deleted runtime, revoked IAM permissions, service outage), the circuit breaker:

1. Disables the SQS event source mapping to stop processing.
2. Sends an alert to the SNS alerts topic with the failure reason and re-enable instructions.

Messages accumulate in the queue until the issue is resolved and the event source is re-enabled (manually).

## Project Structure

```
├── bin/                        CDK app entry point
├── config.example.ts              Global configuration template (copy to config.ts)
├── lib/                        CDK stack definitions
│   ├── agents/                 Agent prompt definitions (deployed to S3)
│   ├── foundation-stack.ts     DynamoDB tables, S3 buckets, SNS alerts, permissions boundary
│   ├── ingestion-stack.ts      Step Function, crawler, processor, DLQ
│   ├── evaluation-stack.ts     AWANA agent on AgentCore, evaluation state machine
│   ├── frontend-stack.ts       CloudFront, Lambda@Edge, Cognito, API Gateway
│   └── layers/                 Lambda layer definitions (boto3)
├── src/                        Runtime source code
│   ├── agents/awana/           AWANA agent (Python, Bedrock AgentCore)
│   ├── billing/                Cost Explorer billing dimensions Lambda
│   ├── consolidation/          Inventory + billing consolidation Lambda
│   ├── crawlers/               RSS crawler (Python Lambda)
│   ├── evaluation/             Evaluation pipeline Lambda (Step Functions)
│   ├── frontend/               Website, edge auth, feedback, preferences, accounts
│   ├── processor/              Announcement processor Lambda
│   └── resource-inventory/     Resource Explorer inventory Lambda
├── scripts/                    Deployment, setup, and teardown helpers
    ├── project-setup.sh        Initial deployment script
    └── delete-app.sh           Two-phase teardown script
```

## Security

This is sample code, please review carefully. If you find a security issue, see [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT License. See the LICENSE file.
