// ─────────────────────────────────────────────────────────────────────────────
// SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
// This code is provided as a reference implementation only.
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Evaluation Stack — deploys the agent runtime and the evaluation
 * pipeline that consumes announcements from the ingestion queue.
 *
 * Merges what was previously the agent stack + the evaluation half of
 * IngestionPipelineStack into a single deployment unit so the agent
 * and its consumers deploy atomically.
 */
import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as custom from 'aws-cdk-lib/custom-resources';
import * as s3assets from 'aws-cdk-lib/aws-s3-assets';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as lambdaEventSources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cw_actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as events from 'aws-cdk-lib/aws-events';
import * as events_targets from 'aws-cdk-lib/aws-events-targets';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import * as path from 'path';
import { Construct } from 'constructs';
import { GlobalConfig, AgentConfig, FeedbackConfig, LambdaConfig } from '../config';
import { AGENT_PROMPTS, SYSTEM_PROMPT } from './agents/prompts';
import { suppressEvaluationFindings } from './nag-suppressions';

export interface EvaluationStackProps extends cdk.StackProps {
  preferencesTable: dynamodb.ITable;
  resultsTable: dynamodb.ITable;
  inventoryBucket: s3.IBucket;
  promptsBucket: s3.IBucket;
  announcementsQueue: sqs.IQueue;
  alertsTopic?: sns.ITopic;
  boto3Layer: lambda.ILayerVersion;
}

export class EvaluationStack extends cdk.Stack {
  public readonly runtimeArn: string;
  public readonly memoryId: string;

  constructor(scope: Construct, id: string, props: EvaluationStackProps) {
    super(scope, id, {
      ...props,
      description: `${GlobalConfig.deploymentPrefix} agent runtime and evaluation pipeline`,
      tags: props?.tags,
    });

    // ═══════════════════════════════════════════════════════════════════
    // SECTION 1: Agent Runtime
    // ═══════════════════════════════════════════════════════════════════

    // ─── S3: deploy agent prompts as JSON files ───

    const promptsData: Record<string, { prompt: string; model_id: string }> = {};
    Object.entries(AGENT_PROMPTS).forEach(([_key, agentPrompt]) => {
      promptsData[agentPrompt.id] = {
        prompt: agentPrompt.prompt,
        model_id: agentPrompt.modelId,
      };
    });

    const promptsManifest = JSON.stringify({
      system_prompt: SYSTEM_PROMPT,
      agents: promptsData,
    }, null, 2);

    new s3deploy.BucketDeployment(this, 'PromptsDeployment', {
      sources: [s3deploy.Source.data('prompts.json', promptsManifest)],
      destinationBucket: props.promptsBucket,
      destinationKeyPrefix: 'config',
      prune: false, // Don't delete other objects in the bucket
    });

    // ─── S3 Asset: bundle agent code with arm64 pip dependencies ───

    const agentAsset = new s3assets.Asset(this, 'AgentCodeZip', {
      path: path.join(__dirname, '..', 'src', 'agents', 'awana'),
      exclude: ['__pycache__', '*.pyc', '.python-version', 'Dockerfile', 'build-and-push.sh', 'README.md'],
      bundling: {
        image: cdk.DockerImage.fromRegistry('public.ecr.aws/sam/build-python3.13:latest-arm64'),
        command: [
          'bash', '-c',
          'pip install -r requirements.txt -t /asset-output --platform manylinux2014_aarch64 --only-binary=:all: --python-version 3.13 && cp agent.py /asset-output/ && find /asset-output -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true',
        ],
        local: {
          tryBundle(outputDir: string) {
            const { execSync } = require('child_process');
            try { execSync('pip3 --version'); } catch { return false; }
            const srcDir = path.join(__dirname, '..', 'src', 'agents', 'awana');
            execSync(
              `pip3 install -r requirements.txt -t "${outputDir}" --platform manylinux2014_aarch64 --only-binary=:all: --python-version 3.13 --quiet && cp agent.py "${outputDir}/" && find "${outputDir}" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true`,
              { cwd: srcDir },
            );
            return true;
          },
        },
      },
    });

    // ─── IAM Role for AgentCore runtime ───

    const runtimeRole = new iam.Role(this, 'RuntimeRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com').withConditions({
        StringEquals: { 'aws:SourceAccount': this.account },
      }),
    });
    runtimeRole.addManagedPolicy(this.createRuntimeBasePolicy('AgentRuntime', AgentConfig));
    agentAsset.grantRead(runtimeRole);

    const allRuntimeRoleArns = [runtimeRole.roleArn];

    runtimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject'],
      resources: [props.promptsBucket.arnForObjects('config/*')],
    }));
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['ce:GetCostAndUsage', 'ce:GetDimensionValues'],
      resources: ['*'],
    }));
    props.preferencesTable.grantReadData(runtimeRole);
    props.inventoryBucket.grantRead(runtimeRole);

    // ─── AgentCore Memory ───

    const memory = new cdk.CfnResource(this, 'PreferenceMemory', {
      type: 'AWS::BedrockAgentCore::Memory',
      properties: {
        Name: FeedbackConfig.memoryName,
        Description: 'Stores learned preferences from announcement feedback',
        EventExpiryDuration: 90,
        MemoryStrategies: [{
          UserPreferenceMemoryStrategy: {
            Name: 'FeedbackPreferenceLearner',
            Namespaces: ['/preferences/{actorId}'],
          },
        }],
        Tags: { project: GlobalConfig.deploymentPrefix },
      },
    });

    const memoryId = memory.ref;

    runtimeRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'bedrock-agentcore:GetMemory',
        'bedrock-agentcore:CreateEvent',
        'bedrock-agentcore:ListEvents',
        'bedrock-agentcore:ListSessions',
        'bedrock-agentcore:ListActors',
        'bedrock-agentcore:RetrieveMemoryRecords',
        'bedrock-agentcore:ListMemoryRecords',
      ],
      resources: [
        `arn:aws:bedrock-agentcore:${this.region}:${this.account}:memory/${memoryId}`,
      ],
    }));

    // ─── AgentCore Runtime (custom resource) ───

    const agentRuntime = new custom.AwsCustomResource(this, 'AgentRuntime', {
      onCreate: {
        service: 'bedrock-agentcore-control',
        action: 'CreateAgentRuntime',
        parameters: {
          agentRuntimeName: AgentConfig.runtimeName,
          description: AgentConfig.description,
          agentRuntimeArtifact: {
            codeConfiguration: {
              code: { s3: { bucket: agentAsset.s3BucketName, prefix: agentAsset.s3ObjectKey } },
              runtime: 'PYTHON_3_13',
              entryPoint: ['agent.py'],
            },
          },
          environmentVariables: {
            AWS_REGION: this.region,
            PROMPTS_BUCKET: props.promptsBucket.bucketName,
            PROMPTS_KEY: 'config/prompts.json',
            LOG_LEVEL: AgentConfig.logLevel,
            PREFERENCES_TABLE: props.preferencesTable.tableName,
            INVENTORY_BUCKET: props.inventoryBucket.bucketName,
            ENABLE_MEMORY: 'true',
            MEMORY_ID: memoryId,
          },
          networkConfiguration: { networkMode: 'PUBLIC' },
          protocolConfiguration: { serverProtocol: 'HTTP' },
          roleArn: runtimeRole.roleArn,
          tags: { type: AgentConfig.type, project: GlobalConfig.deploymentPrefix },
        },
        physicalResourceId: custom.PhysicalResourceId.fromResponse('agentRuntimeId'),
      },
      onUpdate: {
        service: 'bedrock-agentcore-control',
        action: 'UpdateAgentRuntime',
        parameters: {
          agentRuntimeId: new custom.PhysicalResourceIdReference(),
          description: AgentConfig.description,
          agentRuntimeArtifact: {
            codeConfiguration: {
              code: { s3: { bucket: agentAsset.s3BucketName, prefix: agentAsset.s3ObjectKey } },
              runtime: 'PYTHON_3_13',
              entryPoint: ['agent.py'],
            },
          },
          environmentVariables: {
            AWS_REGION: this.region,
            PROMPTS_BUCKET: props.promptsBucket.bucketName,
            PROMPTS_KEY: 'config/prompts.json',
            LOG_LEVEL: AgentConfig.logLevel,
            PREFERENCES_TABLE: props.preferencesTable.tableName,
            INVENTORY_BUCKET: props.inventoryBucket.bucketName,
            ENABLE_MEMORY: 'true',
            MEMORY_ID: memoryId,
          },
          networkConfiguration: { networkMode: 'PUBLIC' },
          protocolConfiguration: { serverProtocol: 'HTTP' },
          roleArn: runtimeRole.roleArn,
        },
        physicalResourceId: custom.PhysicalResourceId.fromResponse('agentRuntimeId'),
      },
      onDelete: {
        service: 'bedrock-agentcore-control',
        action: 'DeleteAgentRuntime',
        parameters: { agentRuntimeId: new custom.PhysicalResourceIdReference() },
      },
      policy: custom.AwsCustomResourcePolicy.fromStatements([
        // Broad AgentCore permissions — the service requires actions across
        // runtime/*, workload-identity-directory/*, and endpoint/* resources.
        // Scoped to account + region; tighten once the full action set is known.
        new iam.PolicyStatement({
          actions: ['bedrock-agentcore:*'],
          resources: [`arn:aws:bedrock-agentcore:${this.region}:${this.account}:*`],
        }),
        new iam.PolicyStatement({ actions: ['iam:PassRole'], resources: allRuntimeRoleArns }),
        new iam.PolicyStatement({
          actions: ['iam:CreateServiceLinkedRole'],
          resources: [
            `arn:aws:iam::${this.account}:role/aws-service-role/network.bedrock-agentcore.amazonaws.com/*`,
            `arn:aws:iam::${this.account}:role/aws-service-role/runtime-identity.bedrock-agentcore.amazonaws.com/*`,
            `arn:aws:iam::${this.account}:role/aws-service-role/bedrock-agentcore.amazonaws.com/*`,
          ],
          conditions: { StringLike: { 'iam:AWSServiceName': '*.bedrock-agentcore.amazonaws.com' } },
        }),
        new iam.PolicyStatement({
          actions: ['s3:GetObject'],
          resources: [agentAsset.bucket.arnForObjects('*')],
        }),
      ]),
      installLatestAwsSdk: true,
    });

    this.runtimeArn = agentRuntime.getResponseField('agentRuntimeArn');
    this.memoryId = memoryId;

    // ─── Agent log group (created by AgentCore — import and set retention) ───

    const agentRuntimeId = agentRuntime.getResponseField('agentRuntimeId');
    const agentLogGroupName = `/aws/bedrock-agentcore/runtimes/${agentRuntimeId}-DEFAULT`;

    new custom.AwsCustomResource(this, 'AgentLogGroupRetention', {
      onCreate: {
        service: 'cloudwatch-logs',
        action: 'PutRetentionPolicy',
        parameters: {
          logGroupName: agentLogGroupName,
          retentionInDays: GlobalConfig.logRetentionDays,
        },
        physicalResourceId: custom.PhysicalResourceId.of('AgentLogGroupRetention'),
      },
      onUpdate: {
        service: 'cloudwatch-logs',
        action: 'PutRetentionPolicy',
        parameters: {
          logGroupName: agentLogGroupName,
          retentionInDays: GlobalConfig.logRetentionDays,
        },
        physicalResourceId: custom.PhysicalResourceId.of('AgentLogGroupRetention'),
      },
      policy: custom.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          actions: ['logs:PutRetentionPolicy'],
          resources: [`arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*`],
        }),
      ]),
    });

    const agentLogGroup = logs.LogGroup.fromLogGroupName(this, 'AgentLogGroup', agentLogGroupName);

    // ─── Agent runtime error alarm ───

    if (props.alertsTopic) {
      const agentErrorFilter = new logs.MetricFilter(this, 'AgentErrorMetricFilter', {
        logGroup: agentLogGroup,
        filterPattern: logs.FilterPattern.anyTerm('ERROR', 'Traceback', 'Exception'),
        metricNamespace: `${GlobalConfig.deploymentPrefix}/Agent`,
        metricName: 'AgentRuntimeErrors',
        metricValue: '1',
        defaultValue: 0,
      });

      const agentErrorAlarm = new cloudwatch.Alarm(this, 'AgentRuntimeErrorAlarm', {
        metric: agentErrorFilter.metric({ period: cdk.Duration.minutes(5), statistic: 'Sum' }),
        threshold: 1,
        evaluationPeriods: 1,
        alarmDescription: `${GlobalConfig.deploymentPrefix} agent runtime is logging errors`,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      });
      agentErrorAlarm.addAlarmAction(new cw_actions.SnsAction(props.alertsTopic));
    }



    // ═══════════════════════════════════════════════════════════════════
    // SECTION 2: Evaluation Pipeline (formerly in IngestionPipelineStack)
    // ═══════════════════════════════════════════════════════════════════

    const agentRuntimeArnPattern = `arn:aws:bedrock-agentcore:${this.region}:${this.account}:runtime/${AgentConfig.runtimeName}*`;

    // ─── Relevant announcements topic ───

    const relevantAnnouncementsTopic = new sns.Topic(this, 'RelevantAnnouncementsTopic', {
      topicName: `${GlobalConfig.deploymentPrefix}-relevant-announcements`,
      displayName: `${GlobalConfig.deploymentPrefix} Relevant Announcements`,
      masterKey: kms.Alias.fromAliasName(this, 'SnsKey', 'alias/aws/sns'),
    });

    // ─── Legacy Processor (kept for backward compatibility) ───

    const processorFunction = new lambda.Function(this, 'ProcessorFunction', {
      description: 'Legacy processor — kept for backward compatibility',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.lambda_handler',
      code: lambda.Code.fromAsset('src/processor'),
      layers: [props.boto3Layer],
      logGroup: new logs.LogGroup(this, 'ProcessorFnLogs', {
        retention: GlobalConfig.logRetentionDays,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        LOG_LEVEL: LambdaConfig.logLevel,
        AGENT_RUNTIME_ARN: this.runtimeArn,
        TOPIC_ARN: relevantAnnouncementsTopic.topicArn,
        RESULTS_TABLE: props.resultsTable.tableName,
        ALERTS_TOPIC_ARN: props.alertsTopic?.topicArn ?? '',
        PROCESSOR_FUNCTION_NAME: `${GlobalConfig.deploymentPrefix}-processor`,
      },
      functionName: `${GlobalConfig.deploymentPrefix}-processor`,
      timeout: cdk.Duration.minutes(5),
    });

    props.resultsTable.grantWriteData(processorFunction);
    relevantAnnouncementsTopic.grantPublish(processorFunction);
    if (props.alertsTopic) props.alertsTopic.grantPublish(processorFunction);

    processorFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:InvokeAgentRuntime'],
      resources: [agentRuntimeArnPattern],
    }));
    processorFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['lambda:ListEventSourceMappings'],
      resources: [`arn:aws:lambda:${this.region}:${this.account}:function:${GlobalConfig.deploymentPrefix}-processor`],
    }));
    processorFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['lambda:UpdateEventSourceMapping'],
      resources: [`arn:aws:lambda:${this.region}:${this.account}:event-source-mapping:*`],
    }));

    // ─── Evaluation Pipeline Lambda ───

    const evaluationFunction = new lambda.Function(this, 'EvaluationFunction', {
      description: 'Orchestrates the multi-step evaluation pipeline (classify, fan-out, store)',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.lambda_handler',
      code: lambda.Code.fromAsset('src/evaluation'),
      layers: [props.boto3Layer],
      logGroup: new logs.LogGroup(this, 'EvaluationFnLogs', {
        retention: GlobalConfig.logRetentionDays,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        LOG_LEVEL: LambdaConfig.logLevel,
        AGENT_RUNTIME_ARN: this.runtimeArn,
        RESULTS_TABLE: props.resultsTable.tableName,
        INVENTORY_BUCKET: props.inventoryBucket.bucketName,
        PREFERENCES_TABLE: props.preferencesTable.tableName,
        TOPIC_ARN: relevantAnnouncementsTopic.topicArn,
        ALERTS_TOPIC_ARN: props.alertsTopic?.topicArn ?? '',
        CENTRAL_ACCOUNT_ID: cdk.Aws.ACCOUNT_ID,
      },
      timeout: cdk.Duration.minutes(5),
    });

    evaluationFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:InvokeAgentRuntime'],
      resources: [agentRuntimeArnPattern],
    }));
    props.resultsTable.grantWriteData(evaluationFunction);
    props.preferencesTable.grantReadData(evaluationFunction);
    props.inventoryBucket.grantRead(evaluationFunction);
    relevantAnnouncementsTopic.grantPublish(evaluationFunction);
    if (props.alertsTopic) props.alertsTopic.grantPublish(evaluationFunction);

    // ─── Evaluation State Machine ───

    const classifyStep = new tasks.LambdaInvoke(this, 'ClassifyStep', {
      lambdaFunction: evaluationFunction,
      payload: sfn.TaskInput.fromObject({
        '_handler': 'classify',
        'announcement': sfn.JsonPath.objectAt('$.announcement'),
      }),
      outputPath: '$.Payload',
      retryOnServiceExceptions: true,
    });
    classifyStep.addRetry({ errors: ['States.TaskFailed'], maxAttempts: 2, backoffRate: 2 });

    const storeSingleServiceResultsStep = new tasks.LambdaInvoke(this, 'StoreSingleServiceResults', {
      lambdaFunction: evaluationFunction,
      payload: sfn.TaskInput.fromObject({
        '_handler': 'store_single_service',
        'announcement': sfn.JsonPath.objectAt('$.announcement'),
        'matched_service': sfn.JsonPath.stringAt('$.matched_service'),
        'services': sfn.JsonPath.objectAt('$.services'),
        'route': 'single_service',
      }),
      outputPath: '$.Payload',
      retryOnServiceExceptions: true,
    });
    storeSingleServiceResultsStep.addRetry({ errors: ['States.TaskFailed'], maxAttempts: 2, backoffRate: 2 });

    const storeAllAccountsStep = new tasks.LambdaInvoke(this, 'StoreAllAccountsResults', {
      lambdaFunction: evaluationFunction,
      payload: sfn.TaskInput.fromObject({
        '_handler': 'store_all_accounts',
        'announcement': sfn.JsonPath.objectAt('$.announcement'),
        'result': sfn.JsonPath.stringAt('$.result'),
        'reasoning': sfn.JsonPath.stringAt('$.reasoning'),
      }),
      outputPath: '$.Payload',
      retryOnServiceExceptions: true,
    });

    const readGroupsStep = new tasks.LambdaInvoke(this, 'ReadAccountGroups', {
      lambdaFunction: evaluationFunction,
      payload: sfn.TaskInput.fromObject({
        '_handler': 'read_groups',
        'announcement': sfn.JsonPath.objectAt('$.announcement'),
      }),
      outputPath: '$.Payload',
      retryOnServiceExceptions: true,
    });

    const perGroupStep = new tasks.LambdaInvoke(this, 'PerGroupEval', {
      lambdaFunction: evaluationFunction,
      payload: sfn.TaskInput.fromObject({
        '_handler': 'per_group',
        'announcement': sfn.JsonPath.objectAt('$.announcement'),
        'group': sfn.JsonPath.objectAt('$.group'),
      }),
      outputPath: '$.Payload',
      retryOnServiceExceptions: true,
    });
    perGroupStep.addRetry({ errors: ['States.TaskFailed'], maxAttempts: 2, backoffRate: 2 });

    const fanOutMap = new sfn.Map(this, 'FanOutAccountGroups', {
      maxConcurrency: GlobalConfig.fanOutMaxConcurrency,
      itemsPath: '$.account_groups',
      resultPath: '$.fan_out_results',
      itemSelector: {
        'announcement': sfn.JsonPath.objectAt('$.announcement'),
        'group': sfn.JsonPath.objectAt('$$.Map.Item.Value'),
      },
    });
    fanOutMap.itemProcessor(perGroupStep);

    const doneState = new sfn.Succeed(this, 'EvaluationDone');

    const classifyDecision = new sfn.Choice(this, 'ClassifyDecision')
      .when(
        sfn.Condition.stringEquals('$.decision', 'relevant_all'),
        storeAllAccountsStep.next(doneState),
      )
      .when(
        sfn.Condition.stringEquals('$.decision', 'not_relevant_all'),
        storeAllAccountsStep,
      )
      .when(
        sfn.Condition.stringEquals('$.decision', 'single_service'),
        storeSingleServiceResultsStep.next(doneState),
      )
      .otherwise(
        readGroupsStep.next(fanOutMap).next(doneState),
      );

    classifyStep.next(classifyDecision);

    const evaluationStateMachine = new sfn.StateMachine(this, 'EvaluationStateMachine', {
      stateMachineName: `${GlobalConfig.deploymentPrefix}-evaluation-pipeline`,
      definitionBody: sfn.DefinitionBody.fromChainable(classifyStep),
      timeout: cdk.Duration.minutes(15),
      logs: {
        destination: new logs.LogGroup(this, 'EvaluationStateMachineLogs', {
          retention: GlobalConfig.logRetentionDays,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
        level: sfn.LogLevel.ERROR,
      },
    });

    // ─── SQS Trigger Lambda ───

    const sqsTriggerFunctionName = `${GlobalConfig.deploymentPrefix}-sqs-trigger`;

    const sqsTriggerFunction = new lambda.Function(this, 'SqsTriggerFunction', {
      functionName: sqsTriggerFunctionName,
      description: 'Receives SQS announcements and starts the evaluation state machine',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.sqs_trigger_handler',
      code: lambda.Code.fromAsset('src/evaluation'),
      layers: [props.boto3Layer],
      logGroup: new logs.LogGroup(this, 'SqsTriggerFnLogs', {
        retention: GlobalConfig.logRetentionDays,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        LOG_LEVEL: LambdaConfig.logLevel,
        STATE_MACHINE_ARN: evaluationStateMachine.stateMachineArn,
        // Required by module-level init in evaluation/index.py
        AGENT_RUNTIME_ARN: this.runtimeArn,
        RESULTS_TABLE: props.resultsTable.tableName,
        INVENTORY_BUCKET: props.inventoryBucket.bucketName,
        PREFERENCES_TABLE: props.preferencesTable.tableName,
        TOPIC_ARN: relevantAnnouncementsTopic.topicArn,
        ALERTS_TOPIC_ARN: props.alertsTopic?.topicArn ?? '',
        CENTRAL_ACCOUNT_ID: cdk.Aws.ACCOUNT_ID,
      },
      timeout: cdk.Duration.minutes(1),
    });

    evaluationStateMachine.grantStartExecution(sqsTriggerFunction);

    // Wire SQS from the ingestion stack to the trigger Lambda.
    // Starts DISABLED — enabled by a custom resource after the full stack is up.
    const sqsEventSource = new lambdaEventSources.SqsEventSource(props.announcementsQueue, {
      batchSize: 1,
      enabled: false,
    });
    sqsTriggerFunction.addEventSource(sqsEventSource);

    // ─── Circuit breaker: evaluation Lambda can disable the SQS trigger ───

    evaluationFunction.addEnvironment('SQS_TRIGGER_FUNCTION_NAME', sqsTriggerFunctionName);

    // Use a string ARN instead of sqsTriggerFunction.functionArn to avoid
    // a circular dependency: EvalFunction → EvalPolicy → SqsTriggerFunction
    // → SqsTriggerPolicy → StateMachine → StateMachinePolicy → EvalFunction.
    evaluationFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['lambda:ListEventSourceMappings'],
      resources: [
        `arn:aws:lambda:${this.region}:${this.account}:function:${sqsTriggerFunctionName}`,
      ],
    }));
    evaluationFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['lambda:UpdateEventSourceMapping'],
      resources: [`arn:aws:lambda:${this.region}:${this.account}:event-source-mapping:*`],
    }));

    // ─── Lambda error + throttle alarms ───

    const lambdasToMonitor: { fn: lambda.Function; name: string }[] = [
      { fn: processorFunction, name: 'Processor' },
      { fn: evaluationFunction, name: 'Evaluation' },
      { fn: sqsTriggerFunction, name: 'SqsTrigger' },
    ];

    if (props.alertsTopic) {
      for (const { fn, name } of lambdasToMonitor) {
        const alarm = new cloudwatch.Alarm(this, `${name}ErrorAlarm`, {
          metric: fn.metricErrors({ period: cdk.Duration.minutes(5) }),
          threshold: 1,
          evaluationPeriods: 1,
          alarmDescription: `${GlobalConfig.deploymentPrefix} ${name} Lambda error rate exceeded threshold`,
          treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
          comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        });
        alarm.addAlarmAction(new cw_actions.SnsAction(props.alertsTopic));

        const throttleAlarm = new cloudwatch.Alarm(this, `${name}ThrottleAlarm`, {
          metric: fn.metricThrottles({ period: cdk.Duration.minutes(5) }),
          threshold: 1,
          evaluationPeriods: 1,
          alarmDescription: `${GlobalConfig.deploymentPrefix} ${name} Lambda is being throttled`,
          treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
          comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        });
        throttleAlarm.addAlarmAction(new cw_actions.SnsAction(props.alertsTopic));
      }
    }

    // ─── Evaluation pipeline failure notification ───

    if (props.alertsTopic) {
      new events.Rule(this, 'EvaluationFailureRule', {
        eventPattern: {
          source: ['aws.states'],
          detailType: ['Step Functions Execution Status Change'],
          detail: {
            stateMachineArn: [evaluationStateMachine.stateMachineArn],
            status: ['FAILED', 'TIMED_OUT', 'ABORTED'],
          },
        },
      }).addTarget(new events_targets.SnsTopic(props.alertsTopic, {
        message: events.RuleTargetInput.fromText(
          `${GlobalConfig.deploymentPrefix} evaluation pipeline execution ${events.EventField.fromPath('$.detail.status')}.` +
          ` Execution ARN: ${events.EventField.fromPath('$.detail.executionArn')}`,
        ),
      }));
    }

    // ─── Enable SQS event source after the full stack is deployed ───
    // The mapping is created disabled so messages aren't consumed before the
    // state machine, agent runtime, and alarms are ready. A custom resource
    // enables it once all critical resources exist.

    const enableMappingFn = new lambda.Function(this, 'EnableSqsEventSourceFn', {
      description: 'Enables the SQS event source mapping after the evaluation stack is fully deployed',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.handler',
      layers: [props.boto3Layer],
      timeout: cdk.Duration.seconds(30),
      logGroup: new logs.LogGroup(this, 'EnableSqsEventSourceFnLogs', {
        retention: GlobalConfig.logRetentionDays,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        FUNCTION_NAME: sqsTriggerFunctionName,
      },
      code: lambda.Code.fromInline(`
import boto3, os, json, logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def handler(event, context):
    request_type = event.get("RequestType", "")
    fn_name = os.environ["FUNCTION_NAME"]
    client = boto3.client("lambda")

    if request_type in ("Create", "Update"):
        mappings = client.list_event_source_mappings(FunctionName=fn_name)
        for m in mappings.get("EventSourceMappings", []):
            if m["State"] in ("Disabled", "Disabling"):
                logger.info("Enabling event source mapping %s", m["UUID"])
                client.update_event_source_mapping(UUID=m["UUID"], Enabled=True)
    # On Delete we leave the mapping as-is — stack deletion handles cleanup

    return {"PhysicalResourceId": f"enable-esm-{fn_name}", "Data": {}}
`),
    });

    enableMappingFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['lambda:ListEventSourceMappings', 'lambda:UpdateEventSourceMapping'],
      resources: ['*'],
    }));

    const enableMappingProvider = new custom.Provider(this, 'EnableSqsEventSourceProvider', {
      onEventHandler: enableMappingFn,
      logGroup: new logs.LogGroup(this, 'EnableSqsEventSourceProviderLogs', {
        retention: GlobalConfig.logRetentionDays,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });

    const enableMappingCr = new cdk.CustomResource(this, 'EnableSqsEventSource', {
      serviceToken: enableMappingProvider.serviceToken,
      properties: {
        // Changing this forces re-evaluation on every deploy
        FunctionName: sqsTriggerFunctionName,
      },
    });

    // Ensure the enabler runs after everything critical is ready
    enableMappingCr.node.addDependency(sqsTriggerFunction);
    enableMappingCr.node.addDependency(evaluationStateMachine);
    enableMappingCr.node.addDependency(evaluationFunction);
    enableMappingCr.node.addDependency(agentRuntime);

    suppressEvaluationFindings(this, AgentConfig.runtimeName);
  }

  /** Creates the base IAM managed policy for an AgentCore runtime. */
  private createRuntimeBasePolicy(id: string, config: { runtimeName: string }): iam.ManagedPolicy {
    return new iam.ManagedPolicy(this, `${id}BasePolicy`, {
      statements: [
        new iam.PolicyStatement({
          actions: ['logs:DescribeLogStreams', 'logs:CreateLogGroup'],
          resources: [`arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*`],
        }),
        new iam.PolicyStatement({
          actions: ['logs:DescribeLogGroups'],
          resources: [`arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/${config.runtimeName}*`],
        }),
        new iam.PolicyStatement({
          actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
          resources: [`arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*`],
        }),
        new iam.PolicyStatement({
          actions: [
            'xray:PutTraceSegments', 'xray:PutTelemetryRecords',
            'xray:GetSamplingRules', 'xray:GetSamplingTargets',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          actions: ['cloudwatch:PutMetricData'],
          resources: ['*'],
          conditions: { StringEquals: { 'cloudwatch:namespace': 'bedrock-agentcore' } },
        }),
        new iam.PolicyStatement({
          sid: 'GetAgentAccessToken',
          actions: [
            'bedrock-agentcore:GetWorkloadAccessToken',
            'bedrock-agentcore:GetWorkloadAccessTokenForJWT',
            'bedrock-agentcore:GetWorkloadAccessTokenForUserId',
          ],
          resources: [
            `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default`,
            `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default/workload-identity/${config.runtimeName}-*`,
          ],
        }),
        // ─── Bedrock model invocation (Global cross-Region inference profile) ───
        // Best-practice policy for global CRIS inference profiles. The three
        // statements together grant access to:
        //   1. The global inference profile resource itself (region-scoped)
        //   2. The in-region foundation-model ARN that the profile dispatches to
        //   3. The regionless foundation-model ARN used for global routing
        // Each statement is conditioned to the specific inference profile, so
        // expanding the model fleet is an additive change.
        // See: https://docs.aws.amazon.com/bedrock/latest/userguide/global-cross-region-inference.html
        ...this.buildBedrockGlobalCrisStatements(GlobalConfig.inferenceProfileId),
      ],
    });
  }

  /**
   * Builds the three IAM statements required to invoke a global cross-Region
   * inference profile (model IDs prefixed with `global.`).
   *
   * @param inferenceProfileId The full profile ID, e.g. `global.anthropic.claude-haiku-4-5-20251001-v1:0`.
   */
  private buildBedrockGlobalCrisStatements(inferenceProfileId: string): iam.PolicyStatement[] {
    const GLOBAL_PREFIX = 'global.';
    if (!inferenceProfileId.startsWith(GLOBAL_PREFIX)) {
      throw new Error(
        `Expected a global cross-Region inference profile ID prefixed with "global." but got "${inferenceProfileId}". ` +
        `Update GlobalConfig.inferenceProfileId or extend buildBedrockGlobalCrisStatements to handle non-global profiles.`,
      );
    }
    const modelName = inferenceProfileId.substring(GLOBAL_PREFIX.length);
    const inferenceProfileArn = `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/${inferenceProfileId}`;

    return [
      new iam.PolicyStatement({
        sid: 'GrantGlobalCrisInferenceProfileRegionAccess',
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: [inferenceProfileArn],
        conditions: {
          StringEquals: { 'aws:RequestedRegion': this.region },
        },
      }),
      new iam.PolicyStatement({
        sid: 'GrantGlobalCrisInferenceProfileInRegionModelAccess',
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: [`arn:aws:bedrock:${this.region}::foundation-model/${modelName}`],
        conditions: {
          StringEquals: {
            'aws:RequestedRegion': this.region,
            'bedrock:InferenceProfileArn': inferenceProfileArn,
          },
        },
      }),
      new iam.PolicyStatement({
        sid: 'GrantGlobalCrisInferenceProfileGlobalModelAccess',
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: [`arn:aws:bedrock:::foundation-model/${modelName}`],
        conditions: {
          StringEquals: {
            'aws:RequestedRegion': 'unspecified',
            'bedrock:InferenceProfileArn': inferenceProfileArn,
          },
        },
      }),
    ];
  }
}
