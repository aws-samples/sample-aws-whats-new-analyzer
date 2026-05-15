// ─────────────────────────────────────────────────────────────────────────────
// SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
// This code is provided as a reference implementation only.
// ─────────────────────────────────────────────────────────────────────────────

// ─── Global settings ───

export const GlobalConfig = {
  /** AWS region for deployment. Resolved from CDK context (-c region=...). */
  deploymentRegion: '',
  /** Prefix for all resource names, enabling multiple deployments in the same account. Resolved from CDK context (-c prefix=...). */
  deploymentPrefix: '',
  /** ARN of the Resource Explorer view used for inventory discovery. Resolved from CDK context (-c resourceExplorerViewArn=...). */
  resourceExplorerViewArn: '',
  /** CloudWatch log retention in days for all components */
  logRetentionDays: 30,
  /** Maximum concurrency for the Step Functions Map state fan-out over Account_Groups. */
  fanOutMaxConcurrency: 5,
  /** Bedrock cross-region inference profile used by all agents. */
  inferenceProfileId: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
};

// ─── Helper: prefix-aware name builders ───

/** Returns the deployment prefix. Call only after GlobalConfig.deploymentPrefix is resolved. */
const prefix = () => GlobalConfig.deploymentPrefix;

/** Returns a lowercase version of the prefix for resources that require it (ECR, Cognito domain, etc.). */
const lowerPrefix = () => prefix().toLowerCase();

/** Converts a prefix to a lowercase snake_case variant (e.g. "My-Prefix" → "my_prefix"). */
const snakePrefix = () => lowerPrefix().replace(/-/g, '_');

// ─── AgentCore runtimes ───

export const AgentConfig = {
  type: 'agent',
  get runtimeName() { return `${snakePrefix()}_agent_runtime`; },
  description: 'AI agent that evaluates AWS announcements for customer relevance using a multi-agent pipeline',
  logLevel: 'ERROR',
};

// ─── Frontend ───

export const FrontendConfig = {
  allowedEmailDomains: [] as string[],
  /** Explicit name for the Lambda@Edge auth function (required because Edge functions need stable names) */
  get edgeAuthFunctionName() { return `${prefix()}-cf-edge-auth-1`; },
};

// ─── API Gateway ───

export const ApiGatewayConfig = {
  /** Steady-state request rate limit (requests per second) across all methods. */
  throttlingRateLimit: 50,
  /** Maximum burst capacity (concurrent requests) before throttling kicks in. */
  throttlingBurstLimit: 100,
};

// ─── Feedback ───

export const FeedbackConfig = {
  /** Name of the AgentCore Memory resource. Derived from the deployment prefix. */
  get memoryName() { return `${GlobalConfig.deploymentPrefix.replace(/-/g, '_')}_preference_memory`; },
};

// ─── Lambda functions ───

export const LambdaConfig = {
  logLevel: 'ERROR',
};
