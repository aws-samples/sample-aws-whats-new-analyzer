// ─────────────────────────────────────────────────────────────────────────────
// SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
// This code is provided as a reference implementation only.
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Foundation Stack — shared data layer, alerts topic, and permissions
 * boundary that all other stacks depend on.
 *
 * Owns: DynamoDB tables, S3 inventory bucket, SNS alerts topic, and
 * the permissions boundary managed policy + CDK Aspect.
 */
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as path from 'path';
import { Construct } from 'constructs';
import { GlobalConfig } from '../config';
import { suppressFoundationFindings } from './nag-suppressions';

export class FoundationStack extends cdk.Stack {
  public readonly preferencesTable: dynamodb.Table;
  public readonly resultsTable: dynamodb.Table;
  public readonly inventoryBucket: s3.Bucket;
  public readonly promptsBucket: s3.Bucket;
  public readonly alertsTopic: sns.Topic;
  public readonly boto3Layer: lambda.LayerVersion;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, {
      ...props,
      description: `Shared data layer, alerts, and permissions boundary for ${GlobalConfig.deploymentPrefix}`,
      tags: props?.tags,
    });

    const prefix = this.node.tryGetContext('deploymentPrefix') || GlobalConfig.deploymentPrefix;

    // ─── DynamoDB tables ───

    this.preferencesTable = new dynamodb.Table(this, 'CustomerPreferencesTable', {
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
    });

    this.resultsTable = new dynamodb.Table(this, 'ResultsTable', {
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      timeToLiveAttribute: 'ttl',
    });

    // GSI for chronological pagination of results in the UI.
    // Items are written with gsi_pk = "ALL" and pubDate = announcement.pubDate
    // so the frontend can Query newest-first with ScanIndexForward=false and
    // paginate via LastEvaluatedKey. Items missing pubDate are simply absent
    // from this index. A constant partition key concentrates writes on a single
    // partition; acceptable for current expected volume (a few hundred
    // announcements/day across N accounts). If volume grows, shard the PK by
    // month (e.g. gsi_pk = pubDate[:7]).
    this.resultsTable.addGlobalSecondaryIndex({
      indexName: 'ByPubDate',
      partitionKey: { name: 'gsi_pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'pubDate', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ─── S3 inventory bucket ───

    this.inventoryBucket = new s3.Bucket(this, 'ResourceInventoryBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      versioned: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      lifecycleRules: [
        { noncurrentVersionExpiration: cdk.Duration.days(7) },
      ],
    });

    // ─── S3 prompts bucket ───

    this.promptsBucket = new s3.Bucket(this, 'PromptsBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      versioned: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      intelligentTieringConfigurations: [{
        name: 'default',
        archiveAccessTierTime: cdk.Duration.days(90),
        deepArchiveAccessTierTime: cdk.Duration.days(180),
      }],
      lifecycleRules: [
        { noncurrentVersionExpiration: cdk.Duration.days(30) },
      ],
    });

    // ─── SNS alerts topic ───

    this.alertsTopic = new sns.Topic(this, 'AlertsTopic', {
      topicName: `${prefix}-alerts`,
      displayName: `${prefix} Alerts`,
      masterKey: kms.Alias.fromAliasName(this, 'SnsKey', 'alias/aws/sns'),
    });

    new cdk.CfnOutput(this, 'AlertsTopicArn', {
      value: this.alertsTopic.topicArn,
      exportName: `${prefix}-AlertsTopicArn`,
    });

    // ─── Shared boto3 Lambda layer ───
    // Pinned version of boto3 shared across all Python Lambda functions.
    // Lambda@Edge and AgentCore runtimes are excluded (different packaging).
    this.boto3Layer = new lambda.LayerVersion(this, 'Boto3Layer', {
      layerVersionName: `${prefix}-boto3`,
      description: 'Pinned boto3 dependency for all Python Lambda functions',
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11, lambda.Runtime.PYTHON_3_12],
      code: lambda.Code.fromAsset(path.join(__dirname, 'layers', 'boto3'), {
        bundling: {
          image: lambda.Runtime.PYTHON_3_11.bundlingImage,
          command: [
            'bash', '-c',
            'pip install -r requirements.txt -t /asset-output/python --quiet',
          ],
          local: {
            tryBundle(outputDir: string) {
              const { execSync } = require('child_process');
              try { execSync('pip3 --version'); } catch { return false; }
              execSync(
                `pip3 install -r requirements.txt -t "${outputDir}/python" --quiet`,
                { cwd: path.join(__dirname, 'layers', 'boto3') },
              );
              return true;
            },
          },
        },
      }),
    });

    // ─── Auto-register Central_Account on deploy ───
    // Lives in the foundation stack so the account record exists before the
    // ingestion schedule rule or evaluation pipeline can fire.

    const registerCentralAccountFn = new lambda.Function(this, 'RegisterCentralAccountFn', {
      description: 'Custom resource: auto-registers the Central_Account in the Account_Registry',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.handler',
      code: lambda.Code.fromAsset('src/frontend/register-central-account'),
      layers: [this.boto3Layer],
      logGroup: new logs.LogGroup(this, 'RegisterCentralAccountFnLogs', {
        retention: GlobalConfig.logRetentionDays,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        LOG_LEVEL: 'INFO',
        PREFERENCES_TABLE: this.preferencesTable.tableName,
        CENTRAL_ACCOUNT_ID: cdk.Aws.ACCOUNT_ID,
        DISPLAY_NAME: prefix,
      },
      timeout: cdk.Duration.seconds(30),
    });

    this.preferencesTable.grantReadWriteData(registerCentralAccountFn);

    registerCentralAccountFn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'AllowDescribeAccountForDisplayName',
      actions: ['organizations:DescribeAccount'],
      resources: ['*'],
    }));

    const registerCentralAccountProvider = new cr.Provider(this, 'RegisterCentralAccountProvider', {
      onEventHandler: registerCentralAccountFn,
      logGroup: new logs.LogGroup(this, 'RegisterCentralAccountProviderLogs', {
        retention: GlobalConfig.logRetentionDays,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });

    new cdk.CustomResource(this, 'RegisterCentralAccount', {
      serviceToken: registerCentralAccountProvider.serviceToken,
      properties: {
        AccountId: cdk.Aws.ACCOUNT_ID,
        // Force update on every deploy so the display name is refreshed from Organizations
        DeployTimestamp: Date.now().toString(),
      },
    });

    // ─── Permissions boundary ───

    const boundaryPolicyName = `${prefix}-created-role-boundary`;
    const boundary = new iam.ManagedPolicy(this, 'CreatedRoleBoundary', {
      managedPolicyName: boundaryPolicyName,
      document: new iam.PolicyDocument({
        statements: [
          new iam.PolicyStatement({
            sid: 'BoundaryAllowCeiling',
            effect: iam.Effect.ALLOW,
            actions: ['*'],
            resources: ['*'],
          }),
          new iam.PolicyStatement({
            sid: 'DenyEscalationActions',
            effect: iam.Effect.DENY,
            actions: [
              'iam:CreateRole',
              'iam:CreateUser',
              'iam:CreatePolicy',
              'iam:CreatePolicyVersion',
              'iam:AttachRolePolicy',
              'iam:AttachUserPolicy',
              'iam:AttachGroupPolicy',
              'iam:PutRolePolicy',
              'iam:PutUserPolicy',
              'iam:PutGroupPolicy',
              'iam:DeleteRolePermissionsBoundary',
              'iam:DeleteUserPermissionsBoundary',
              'iam:UpdateAssumeRolePolicy',
              'iam:CreateAccessKey',
              'iam:CreateLoginProfile',
              'iam:UpdateLoginProfile',
            ],
            resources: ['*'],
          }),
          new iam.PolicyStatement({
            sid: 'DenyBoundaryTampering',
            effect: iam.Effect.DENY,
            actions: [
              'iam:DeletePolicy',
              'iam:DeletePolicyVersion',
              'iam:SetDefaultPolicyVersion',
              'iam:CreatePolicyVersion',
            ],
            resources: [`arn:aws:iam::${cdk.Aws.ACCOUNT_ID}:policy/${boundaryPolicyName}`],
          }),
        ],
      }),
    });

    const boundaryArn = `arn:aws:iam::${cdk.Aws.ACCOUNT_ID}:policy/${boundaryPolicyName}`;

    // Apply the boundary to every IAM role across all stacks via a CDK Aspect.
    const boundaryCfn = boundary.node.defaultChild as cdk.CfnResource;
    cdk.Aspects.of(this.node.root).add({
      visit(node: Construct) {
        if (node instanceof iam.CfnRole) {
          node.addPropertyOverride('PermissionsBoundary', boundaryArn);
          if (cdk.Stack.of(node) === cdk.Stack.of(boundaryCfn)) {
            node.addDependency(boundaryCfn);
          }
        } else if (
          node instanceof cdk.CfnResource &&
          node.cfnResourceType === 'AWS::IAM::Role'
        ) {
          node.addPropertyOverride('PermissionsBoundary', boundaryArn);
          if (cdk.Stack.of(node) === cdk.Stack.of(boundaryCfn)) {
            node.addDependency(boundaryCfn);
          }
        }
      },
    });

    suppressFoundationFindings(this);
  }
}
