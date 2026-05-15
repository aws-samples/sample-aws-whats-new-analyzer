// ─────────────────────────────────────────────────────────────────────────────
// SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
// This code is provided as a reference implementation only.
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Ingestion Stack — orchestrates resource inventory collection, billing
 * dimension extraction, consolidation, and AWS What's New RSS crawling.
 *
 * Produces announcements on an SQS queue consumed by the EvaluationStack.
 * Has no dependency on the agent runtime.
 */
import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as lambdaEventSources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cw_actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as events from 'aws-cdk-lib/aws-events';
import * as events_targets from 'aws-cdk-lib/aws-events-targets';
import * as path from 'path';
import { Rule, Schedule } from 'aws-cdk-lib/aws-events';
import { SfnStateMachine } from 'aws-cdk-lib/aws-events-targets';
import { Construct } from 'constructs';
import { GlobalConfig, LambdaConfig } from '../config';
import { suppressIngestionFindings } from './nag-suppressions';

export interface IngestionStackProps extends cdk.StackProps {
  preferencesTable: dynamodb.ITable;
  inventoryBucket: s3.IBucket;
  alertsTopic: sns.ITopic;
  resourceExplorerViewArn: string;
  boto3Layer: lambda.ILayerVersion;
}

export class IngestionStack extends cdk.Stack {
  /** SQS queue where crawled announcements are published. */
  public readonly announcementsQueue: sqs.IQueue;
  /** Dead-letter queue for failed announcement processing. */
  public readonly announcementsDlq: sqs.IQueue;

  constructor(scope: Construct, id: string, props: IngestionStackProps) {
    super(scope, id, {
      ...props,
      description: 'Step Function pipeline for resource inventory and AWS What\'s New ingestion',
      tags: props?.tags,
    });

    // ─── Resource Inventory Lambda ───

    const inventoryFunction = new lambda.Function(this, 'ResourceInventoryFn', {
      description: 'Collects AWS resource inventory via Resource Explorer and writes to S3',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.lambda_handler',
      code: lambda.Code.fromAsset('src/resource-inventory'),
      layers: [props.boto3Layer],
      logGroup: new logs.LogGroup(this, 'ResourceInventoryLogs', {
        retention: GlobalConfig.logRetentionDays,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        LOG_LEVEL: LambdaConfig.logLevel,
        INVENTORY_BUCKET: props.inventoryBucket.bucketName,
        PREFERENCES_TABLE: props.preferencesTable.tableName,
        CENTRAL_ACCOUNT_ID: cdk.Aws.ACCOUNT_ID,
        RESOURCE_EXPLORER_VIEW_ARN: props.resourceExplorerViewArn,
      },
      timeout: cdk.Duration.minutes(5),
    });

    props.inventoryBucket.grantWrite(inventoryFunction);
    props.preferencesTable.grantReadData(inventoryFunction);

    inventoryFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['resource-explorer-2:Search'],
      resources: [props.resourceExplorerViewArn || 'arn:aws:resource-explorer-2:*:*:view/*/*'],
    }));

    // ─── Consolidation Lambda ───

    const consolidationFunction = new lambda.Function(this, 'ConsolidationFn', {
      description: 'Consolidates per-account inventories and billing dimensions into a single context file',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.lambda_handler',
      code: lambda.Code.fromAsset('src/consolidation'),
      layers: [props.boto3Layer],
      logGroup: new logs.LogGroup(this, 'ConsolidationLogs', {
        retention: GlobalConfig.logRetentionDays,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        LOG_LEVEL: LambdaConfig.logLevel,
        INVENTORY_BUCKET: props.inventoryBucket.bucketName,
        PREFERENCES_TABLE: props.preferencesTable.tableName,
      },
      timeout: cdk.Duration.minutes(5),
    });

    props.inventoryBucket.grantReadWrite(consolidationFunction);
    props.preferencesTable.grantReadData(consolidationFunction);

    // ─── Billing Dimensions Lambda ───

    const billingFunction = new lambda.Function(this, 'BillingDimensionsFn', {
      description: 'Queries Cost Explorer for org-wide billing dimensions and writes to S3',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.lambda_handler',
      code: lambda.Code.fromAsset('src/billing'),
      layers: [props.boto3Layer],
      logGroup: new logs.LogGroup(this, 'BillingDimensionsLogs', {
        retention: GlobalConfig.logRetentionDays,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        LOG_LEVEL: LambdaConfig.logLevel,
        INVENTORY_BUCKET: props.inventoryBucket.bucketName,
      },
      timeout: cdk.Duration.minutes(5),
    });

    props.inventoryBucket.grantWrite(billingFunction);

    billingFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ce:GetDimensionValues', 'ce:GetCostAndUsage'],
      resources: ['*'],
    }));

    // ─── Crawler resources ───

    const dedupTable = new dynamodb.Table(this, 'CrawlerDedupTable', {
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const dlq = new sqs.Queue(this, 'AnnouncementsDLQ', {
      queueName: `${GlobalConfig.deploymentPrefix}-announcements-dlq`,
      visibilityTimeout: cdk.Duration.minutes(2),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    this.announcementsDlq = dlq;

    const announcementsQueue = new sqs.Queue(this, 'AnnouncementsQueue', {
      queueName: `${GlobalConfig.deploymentPrefix}-announcements`,
      visibilityTimeout: cdk.Duration.seconds(300),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      deadLetterQueue: { queue: dlq, maxReceiveCount: 3 },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    this.announcementsQueue = announcementsQueue;

    // Crawler state (last seen pubDate) is stored as a sentinel row in
    // dedupTable, keyed by id='__crawler_state__'. No SSM parameter required.

    const crawlerFunction = new lambda.Function(this, 'ContentCrawlerFunction', {
      description: 'Crawls AWS What\'s New RSS feed and queues new announcements',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.lambda_handler',
      layers: [props.boto3Layer],
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'src', 'crawlers', 'whats-new-crawler'), {
        exclude: ['node_modules', 'package.json', 'package-lock.json', 'bun.lock', '*.ts', 'dlq-handler'],
        bundling: {
          image: lambda.Runtime.PYTHON_3_11.bundlingImage,
          command: [
            'bash', '-c',
            'pip install -r requirements.txt -t /asset-output && cp index.py /asset-output/',
          ],
          local: {
            tryBundle(outputDir: string) {
              const { execSync } = require('child_process');
              try { execSync('pip3 --version'); } catch { return false; }
              execSync(
                `pip3 install -r requirements.txt -t "${outputDir}" --quiet && cp index.py "${outputDir}/"`,
                { cwd: path.join(__dirname, '..', 'src', 'crawlers', 'whats-new-crawler') },
              );
              return true;
            },
          },
        },
      }),
      timeout: cdk.Duration.minutes(5),
      logGroup: new logs.LogGroup(this, 'CrawlerFnLogs', {
        retention: GlobalConfig.logRetentionDays,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        DEDUP_TABLE_NAME: dedupTable.tableName,
        QUEUE_URL: announcementsQueue.queueUrl,
        LOG_LEVEL: LambdaConfig.logLevel,
      },
    });

    dedupTable.grantReadWriteData(crawlerFunction);
    announcementsQueue.grantSendMessages(crawlerFunction);

    // ─── DLQ handler ───

    const dlqHandler = new lambda.Function(this, 'DlqHandlerFunction', {
      description: 'Processes failed announcements from the dead-letter queue',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'src', 'crawlers', 'whats-new-crawler', 'dlq-handler')),
      layers: [props.boto3Layer],
      timeout: cdk.Duration.minutes(1),
      logGroup: new logs.LogGroup(this, 'DlqHandlerFnLogs', {
        retention: GlobalConfig.logRetentionDays,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        DEDUP_TABLE_NAME: dedupTable.tableName,
        LOG_LEVEL: LambdaConfig.logLevel,
      },
    });

    dedupTable.grantReadWriteData(dlqHandler);

    dlqHandler.addEventSource(
      new lambdaEventSources.SqsEventSource(dlq, { batchSize: 1 }),
    );

    // ─── Step Function: [Inventory + Billing] (parallel) → Consolidation → Crawler ───

    const inventoryStep = new tasks.LambdaInvoke(this, 'CollectInventory', {
      lambdaFunction: inventoryFunction,
      outputPath: '$.Payload',
      retryOnServiceExceptions: true,
    });

    const billingStep = new tasks.LambdaInvoke(this, 'CollectBillingDimensions', {
      lambdaFunction: billingFunction,
      outputPath: '$.Payload',
      retryOnServiceExceptions: true,
    });

    const dataCollectionParallel = new sfn.Parallel(this, 'CollectData', {
      comment: 'Collect resource inventory and billing dimensions in parallel',
    });
    dataCollectionParallel.branch(inventoryStep);
    dataCollectionParallel.branch(billingStep);

    const consolidationStep = new tasks.LambdaInvoke(this, 'ConsolidateInventory', {
      lambdaFunction: consolidationFunction,
      outputPath: '$.Payload',
      retryOnServiceExceptions: true,
    });

    const crawlerStep = new tasks.LambdaInvoke(this, 'CrawlWhatsNew', {
      lambdaFunction: crawlerFunction,
      outputPath: '$.Payload',
      retryOnServiceExceptions: true,
    });

    const pipelineDefinition = dataCollectionParallel.next(consolidationStep).next(crawlerStep);

    const stateMachine = new sfn.StateMachine(this, 'IngestionStateMachine', {
      stateMachineName: `${GlobalConfig.deploymentPrefix}-ingestion-pipeline`,
      definitionBody: sfn.DefinitionBody.fromChainable(pipelineDefinition),
      timeout: cdk.Duration.minutes(10),
      logs: {
        destination: new logs.LogGroup(this, 'StateMachineLogs', {
          retention: GlobalConfig.logRetentionDays,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
        level: sfn.LogLevel.ERROR,
      },
    });

    // Schedule the Step Function
    new Rule(this, 'IngestionScheduleRule', {
      schedule: Schedule.cron({ minute: '0', hour: '7', weekDay: 'MON-FRI' }),
    }).addTarget(new SfnStateMachine(stateMachine));

    // ─── Trigger an immediate run on deploy ───

    new cr.AwsCustomResource(this, 'TriggerIngestionOnDeploy', {
      onCreate: {
        service: 'SFN',
        action: 'startExecution',
        parameters: { stateMachineArn: stateMachine.stateMachineArn },
        physicalResourceId: cr.PhysicalResourceId.of(
          `trigger-${stateMachine.stateMachineName}-${Date.now()}`,
        ),
      },
      onUpdate: {
        service: 'SFN',
        action: 'startExecution',
        parameters: { stateMachineArn: stateMachine.stateMachineArn },
        physicalResourceId: cr.PhysicalResourceId.of(
          `trigger-${stateMachine.stateMachineName}-${Date.now()}`,
        ),
      },
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          actions: ['states:StartExecution'],
          resources: [stateMachine.stateMachineArn],
        }),
      ]),
    });

    // ─── Lambda error + throttle alarms ───

    const lambdasToMonitor: { fn: lambda.Function; name: string }[] = [
      { fn: inventoryFunction, name: 'ResourceInventory' },
      { fn: consolidationFunction, name: 'Consolidation' },
      { fn: billingFunction, name: 'BillingDimensions' },
      { fn: crawlerFunction, name: 'ContentCrawler' },
      { fn: dlqHandler, name: 'DlqHandler' },
    ];

    for (const { fn, name } of lambdasToMonitor) {
      const alarm = new cloudwatch.Alarm(this, `${name}ErrorAlarm`, {
        metric: fn.metricErrors({ period: cdk.Duration.minutes(5) }),
        threshold: 3,
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

    // ─── DLQ depth alarm ───

    const dlqDepthAlarm = new cloudwatch.Alarm(this, 'DlqDepthAlarm', {
      metric: dlq.metricApproximateNumberOfMessagesVisible({
        period: cdk.Duration.minutes(5),
      }),
      threshold: 1,
      evaluationPeriods: 1,
      alarmDescription: `${GlobalConfig.deploymentPrefix} DLQ has messages — announcements are failing to process`,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    });
    dlqDepthAlarm.addAlarmAction(new cw_actions.SnsAction(props.alertsTopic));

    // ─── SQS queue age alarm ───

    const queueAgeAlarm = new cloudwatch.Alarm(this, 'QueueAgeAlarm', {
      metric: announcementsQueue.metricApproximateAgeOfOldestMessage({
        period: cdk.Duration.minutes(5),
      }),
      threshold: 900,
      evaluationPeriods: 2,
      alarmDescription: `${GlobalConfig.deploymentPrefix} announcements queue has messages older than 15 minutes — pipeline may be stalled`,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    });
    queueAgeAlarm.addAlarmAction(new cw_actions.SnsAction(props.alertsTopic));

    // ─── Step Function failure notification ───

    new events.Rule(this, 'IngestionFailureRule', {
      eventPattern: {
        source: ['aws.states'],
        detailType: ['Step Functions Execution Status Change'],
        detail: {
          stateMachineArn: [stateMachine.stateMachineArn],
          status: ['FAILED', 'TIMED_OUT', 'ABORTED'],
        },
      },
    }).addTarget(new events_targets.SnsTopic(props.alertsTopic, {
      message: events.RuleTargetInput.fromText(
        `${GlobalConfig.deploymentPrefix} ingestion pipeline execution ${events.EventField.fromPath('$.detail.status')}.` +
        ` Execution ARN: ${events.EventField.fromPath('$.detail.executionArn')}`,
      ),
    }));

    suppressIngestionFindings(this);
  }
}
