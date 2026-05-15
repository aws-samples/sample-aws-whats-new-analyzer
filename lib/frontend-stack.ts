// ─────────────────────────────────────────────────────────────────────────────
// SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
// This code is provided as a reference implementation only.
// ─────────────────────────────────────────────────────────────────────────────

import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as apigw from 'aws-cdk-lib/aws-apigateway';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cw_actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import { DynamoEventSource } from 'aws-cdk-lib/aws-lambda-event-sources';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';
import { FrontendConfig, GlobalConfig, LambdaConfig, FeedbackConfig, ApiGatewayConfig } from '../config';
import { suppressFrontendFindings } from './nag-suppressions';
import * as fs from 'fs';
import * as path from 'path';

interface FrontendStackProps extends cdk.StackProps {
  preferencesTable: dynamodb.Table;
  resultsTable: dynamodb.ITable;
  inventoryBucket?: s3.IBucket;
  promptsBucket?: s3.IBucket;
  memoryId?: string;
  alertsTopic?: sns.ITopic;
  boto3Layer: lambda.ILayerVersion;
}

export class FrontendStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: FrontendStackProps) {
    super(scope, id, {
      ...props,
      description: `CloudFront, Cognito, API Gateway, and Lambda functions for the ${GlobalConfig.deploymentPrefix} frontend`,
      tags: props?.tags,
    });

    // ─── Self-sign-up: enabled only when allowedEmailDomains is non-empty ───
    const allowSelfSignUp = FrontendConfig.allowedEmailDomains.length > 0;

    // Pre Sign-Up Lambda (restrict to allowed email domains)
    const preSignUpFn = allowSelfSignUp
      ? new lambda.Function(this, 'PreSignUpFn', {
          runtime: lambda.Runtime.PYTHON_3_10,
          handler: 'index.handler',
          code: lambda.Code.fromInline([
            'import logging',
            'import os',
            '',
            'log_level = os.environ.get("LOG_LEVEL", "INFO").upper()',
            'logging.basicConfig(',
            '    level=getattr(logging, log_level, logging.INFO),',
            '    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",',
            ')',
            'logger = logging.getLogger(__name__)',
            '',
            'ALLOWED_DOMAINS = [d.strip() for d in os.environ["ALLOWED_EMAIL_DOMAINS"].split(",")]',
            '',
            'def handler(event, context):',
            '    email = event["request"]["userAttributes"].get("email", "")',
            '    logger.info("Pre sign-up check for email: %s", email)',
            '    domain = email.rsplit("@", 1)[-1] if "@" in email else ""',
            '    if domain not in ALLOWED_DOMAINS:',
            '        logger.warning("Rejected sign-up attempt from: %s", email)',
            '        raise Exception(f"Only email addresses from {ALLOWED_DOMAINS} are allowed to register.")',
            '    event["response"]["autoConfirmUser"] = True',
            '    event["response"]["autoVerifyEmail"] = True',
            '    return event',
          ].join('\n')),
          environment: {
            LOG_LEVEL: LambdaConfig.logLevel,
            ALLOWED_EMAIL_DOMAINS: FrontendConfig.allowedEmailDomains.join(','),
          },
          timeout: cdk.Duration.seconds(5),
        })
      : undefined;

    // ─── Cognito User Pool ───
    const userPool = new cognito.UserPool(this, 'UserPool', {
      userPoolName: `${GlobalConfig.deploymentPrefix}-user-pool`,
      selfSignUpEnabled: allowSelfSignUp,
      signInAliases: { email: true },
      autoVerify: { email: true },
      passwordPolicy: {
        minLength: 8,
        requireUppercase: true,
        requireLowercase: true,
        requireDigits: true,
        requireSymbols: true,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      ...(preSignUpFn ? { lambdaTriggers: { preSignUp: preSignUpFn } } : {}),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Self-sign-up is controlled by the UserPool's selfSignUpEnabled property.
    // The pre-sign-up trigger restricts registration to allowed email domains.

    const cognitoDomain = userPool.addDomain('CognitoDomain', {
      cognitoDomain: {
        domainPrefix: `${GlobalConfig.deploymentPrefix.toLowerCase()}-${cdk.Aws.ACCOUNT_ID}`,
      },
    });

    // ─── S3 Bucket for static website ───
    const websiteBucket = new s3.Bucket(this, 'WebsiteBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
    });

    // ─── Lambda@Edge for JWT authentication ───
    // Bake the SSM region into the edge function code at synth time
    // (Lambda@Edge cannot use environment variables)
    const edgeCodeDir = path.join(__dirname, '..', 'src', 'frontend', 'edge-auth');
    const edgeCode = fs.readFileSync(path.join(edgeCodeDir, 'index.py'), 'utf-8')
      .replace(/__SSM_REGION__/g, this.region)
      .replace(/__SSM_PARAM_NAME__/g, `/${GlobalConfig.deploymentPrefix}/edge-auth/config`);

    const edgeAuthFunction = new cloudfront.experimental.EdgeFunction(this, 'EdgeAuthFunction', {
      functionName: FrontendConfig.edgeAuthFunctionName,
      description: 'JWT authentication for CloudFront viewer requests',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline(edgeCode),
      timeout: cdk.Duration.seconds(5),
      memorySize: 128,
    });

    // ─── Origin Access Control (unique name to avoid collisions on redeploy) ───
    const oac = new cloudfront.S3OriginAccessControl(this, 'OAC', {
      originAccessControlName: `${GlobalConfig.deploymentPrefix}-oac-${cdk.Aws.STACK_NAME}-${cdk.Aws.REGION}`,
    });

    // ─── CloudFront Distribution ───
    const distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: `${GlobalConfig.deploymentPrefix} frontend distribution`,
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_3_2025,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(websiteBucket, {
          originAccessControl: oac,
        }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        edgeLambdas: [
          {
            functionVersion: edgeAuthFunction.currentVersion,
            eventType: cloudfront.LambdaEdgeEventType.VIEWER_REQUEST,
          },
        ],
      },
      defaultRootObject: 'index.html',
      errorResponses: [
        {
          httpStatus: 403,
          responseHttpStatus: 200,
          responsePagePath: '/index.html',
        },
        {
          httpStatus: 404,
          responseHttpStatus: 200,
          responsePagePath: '/index.html',
        },
      ],
    });

    const callbackUrl = `https://${distribution.distributionDomainName}`;

    // ─── Cognito App Client ───
    const cfnUserPoolClient = new cognito.CfnUserPoolClient(this, 'AppClient', {
      clientName: `${GlobalConfig.deploymentPrefix}-frontend-client`,
      userPoolId: userPool.userPoolId,
      generateSecret: false,
      allowedOAuthFlows: ['code'],
      allowedOAuthScopes: ['openid', 'email', 'profile'],
      allowedOAuthFlowsUserPoolClient: true,
      callbackUrLs: [callbackUrl],
      logoutUrLs: [callbackUrl],
      supportedIdentityProviders: ['COGNITO'],
      explicitAuthFlows: ['ALLOW_USER_SRP_AUTH', 'ALLOW_REFRESH_TOKEN_AUTH'],
    });

    // ─── SSM Parameter for Lambda@Edge config ───
    // Stores Cognito config so the edge function can load it at runtime
    // instead of requiring a post-deploy script to patch placeholders.
    const edgeAuthConfigParam = new ssm.StringParameter(this, 'EdgeAuthConfig', {
      parameterName: `/${GlobalConfig.deploymentPrefix}/edge-auth/config`,
      description: 'Cognito configuration for Lambda@Edge auth function',
      stringValue: cdk.Fn.join('', [
        '{"cognitoRegion":"', cdk.Aws.REGION,
        '","userPoolId":"', userPool.userPoolId,
        '","clientId":"', cfnUserPoolClient.ref,
        '","cognitoDomain":"', cognitoDomain.baseUrl(),
        '","callbackUrl":"', callbackUrl,
        '","logLevel":"INFO"}',
      ]),
    });

    // Grant the edge function permission to read the SSM parameter
    edgeAuthFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ssm:GetParameter'],
      resources: [
        cdk.Arn.format({
          service: 'ssm',
          region: this.region,
          resource: 'parameter',
          resourceName: `${GlobalConfig.deploymentPrefix}/edge-auth/config`,
        }, this),
      ],
    }));

    // ─── Deploy static website + config.json to S3 ───
    new s3deploy.BucketDeployment(this, 'DeployWebsite', {
      sources: [
        s3deploy.Source.asset('src/frontend/website'),
        s3deploy.Source.jsonData('config.json', {
          clientId: cfnUserPoolClient.ref,
          cognitoDomain: cognitoDomain.baseUrl(),
        }),
      ],
      destinationBucket: websiteBucket,
      distribution,
      distributionPaths: ['/*'],
    });

    // ─── Lambda for preferences CRUD ───
    const preferencesFunction = new lambda.Function(this, 'PreferencesFunction', {
      description: 'CRUD operations for user announcement preferences',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.lambda_handler',
      code: lambda.Code.fromAsset('src/frontend/preferences'),
      layers: [props.boto3Layer],
      logGroup: new logs.LogGroup(this, 'PreferencesFnLogs', {
        retention: GlobalConfig.logRetentionDays,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        LOG_LEVEL: LambdaConfig.logLevel,
        PREFERENCES_TABLE: props.preferencesTable.tableName,
      },
      timeout: cdk.Duration.seconds(10),
    });

    props.preferencesTable.grantReadWriteData(preferencesFunction);

    // ─── API Gateway (REST) with Cognito authorizer ───
    const api = new apigw.RestApi(this, 'BackendApi', {
      restApiName: `${GlobalConfig.deploymentPrefix}-backend-api`,
      description: 'Backend REST API secured by Cognito',
      deployOptions: {
        stageName: 'prod',
        throttlingRateLimit: ApiGatewayConfig.throttlingRateLimit,
        throttlingBurstLimit: ApiGatewayConfig.throttlingBurstLimit,
      },
      defaultCorsPreflightOptions: {
        allowOrigins: apigw.Cors.ALL_ORIGINS,
        allowMethods: apigw.Cors.ALL_METHODS,
        allowHeaders: ['Content-Type', 'Authorization'],
      },
    });

    const cognitoAuthorizer = new apigw.CognitoUserPoolsAuthorizer(this, 'CognitoAuthorizer', {
      cognitoUserPools: [userPool],
      authorizerName: `${GlobalConfig.deploymentPrefix}-cognito-authorizer`,
    });

    const lambdaIntegration = new apigw.LambdaIntegration(preferencesFunction);
    const apiResource = api.root.addResource('api');

    const preferencesResource = apiResource.addResource('preferences');
    preferencesResource.addMethod('GET', lambdaIntegration, {
      authorizer: cognitoAuthorizer,
      authorizationType: apigw.AuthorizationType.COGNITO,
    });
    preferencesResource.addMethod('POST', lambdaIntegration, {
      authorizer: cognitoAuthorizer,
      authorizationType: apigw.AuthorizationType.COGNITO,
    });
    preferencesResource.addMethod('PUT', lambdaIntegration, {
      authorizer: cognitoAuthorizer,
      authorizationType: apigw.AuthorizationType.COGNITO,
    });
    preferencesResource.addMethod('DELETE', lambdaIntegration, {
      authorizer: cognitoAuthorizer,
      authorizationType: apigw.AuthorizationType.COGNITO,
    });

    // ─── Lambda for results read access ───
    const resultsFunction = new lambda.Function(this, 'ResultsFunction', {
      description: 'Read access to announcement evaluation results',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.lambda_handler',
      code: lambda.Code.fromAsset('src/frontend/results'),
      layers: [props.boto3Layer],
      logGroup: new logs.LogGroup(this, 'ResultsFnLogs', {
        retention: GlobalConfig.logRetentionDays,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        LOG_LEVEL: LambdaConfig.logLevel,
        RESULTS_TABLE: props.resultsTable.tableName,
      },
      timeout: cdk.Duration.seconds(10),
    });

    props.resultsTable.grantReadData(resultsFunction);

    const resultsResource = apiResource.addResource('results');
    resultsResource.addMethod('GET', new apigw.LambdaIntegration(resultsFunction), {
      authorizer: cognitoAuthorizer,
      authorizationType: apigw.AuthorizationType.COGNITO,
    });

    // ─── Lambda for feedback CRUD ───
    const feedbackFunction = new lambda.Function(this, 'FeedbackFunction', {
      description: 'CRUD operations for user feedback on announcements',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.lambda_handler',
      code: lambda.Code.fromAsset('src/frontend/feedback-analyst'),
      layers: [props.boto3Layer],
      logGroup: new logs.LogGroup(this, 'FeedbackFnLogs', {
        retention: GlobalConfig.logRetentionDays,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        LOG_LEVEL: LambdaConfig.logLevel,
        PREFERENCES_TABLE: props.preferencesTable.tableName,
        RESULTS_TABLE: props.resultsTable.tableName,
      },
      timeout: cdk.Duration.seconds(10),
    });

    props.preferencesTable.grantReadWriteData(feedbackFunction);
    props.resultsTable.grantReadData(feedbackFunction);

    const feedbackIntegration = new apigw.LambdaIntegration(feedbackFunction);
    const feedbackResource = apiResource.addResource('feedback');
    feedbackResource.addMethod('POST', feedbackIntegration, {
      authorizer: cognitoAuthorizer,
      authorizationType: apigw.AuthorizationType.COGNITO,
    });
    feedbackResource.addMethod('GET', feedbackIntegration, {
      authorizer: cognitoAuthorizer,
      authorizationType: apigw.AuthorizationType.COGNITO,
    });
    feedbackResource.addMethod('DELETE', feedbackIntegration, {
      authorizer: cognitoAuthorizer,
      authorizationType: apigw.AuthorizationType.COGNITO,
    });

    // ─── Lambda for accounts registry CRUD ───
    const accountsFunction = new lambda.Function(this, 'AccountsFunction', {
      description: 'CRUD operations for multi-account registry',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.lambda_handler',
      code: lambda.Code.fromAsset('src/frontend/accounts'),
      layers: [props.boto3Layer],
      logGroup: new logs.LogGroup(this, 'AccountsFnLogs', {
        retention: GlobalConfig.logRetentionDays,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        LOG_LEVEL: LambdaConfig.logLevel,
        PREFERENCES_TABLE: props.preferencesTable.tableName,
        CENTRAL_ACCOUNT_ID: cdk.Aws.ACCOUNT_ID,
      },
      timeout: cdk.Duration.seconds(10),
    });

    props.preferencesTable.grantReadWriteData(accountsFunction);

    // Grant Organizations read access for account validation
    accountsFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['organizations:DescribeAccount', 'organizations:ListAccounts'],
      resources: ['*'],
    }));

    const accountsIntegration = new apigw.LambdaIntegration(accountsFunction);
    const accountsResource = apiResource.addResource('accounts');
    accountsResource.addMethod('GET', accountsIntegration, {
      authorizer: cognitoAuthorizer,
      authorizationType: apigw.AuthorizationType.COGNITO,
    });
    accountsResource.addMethod('POST', accountsIntegration, {
      authorizer: cognitoAuthorizer,
      authorizationType: apigw.AuthorizationType.COGNITO,
    });
    accountsResource.addMethod('PUT', accountsIntegration, {
      authorizer: cognitoAuthorizer,
      authorizationType: apigw.AuthorizationType.COGNITO,
    });
    accountsResource.addMethod('DELETE', accountsIntegration, {
      authorizer: cognitoAuthorizer,
      authorizationType: apigw.AuthorizationType.COGNITO,
    });

    // ─── Central_Account registration moved to FoundationStack ───
    // (ensures the account record exists before ingestion/evaluation stacks deploy)

    // ─── Feedback Analyst Lambda + DynamoDB Stream ───

    const feedbackAnalystFunction = new lambda.Function(this, 'FeedbackAnalystFunction', {
        description: 'Analyzes feedback via Bedrock and stores learned preferences in AgentCore Memory',
        runtime: lambda.Runtime.PYTHON_3_11,
        handler: 'index.lambda_handler',
        layers: [props.boto3Layer],
        code: lambda.Code.fromAsset('src/frontend/feedback-analyst', {
          bundling: {
            image: lambda.Runtime.PYTHON_3_11.bundlingImage,
            command: [
              'bash', '-c',
              'pip install -r requirements.txt -t /asset-output && cp -r . /asset-output',
            ],
            local: {
              tryBundle(outputDir: string) {
                const { execSync } = require('child_process');
                try {
                  execSync('pip3 --version');
                } catch {
                  return false; // pip3 not available — fall back to Docker
                }
                execSync(
                  `pip3 install -r requirements.txt -t "${outputDir}" --quiet && cp -r . "${outputDir}"`,
                  { cwd: path.join(__dirname, '..', 'src', 'frontend', 'feedback-analyst') },
                );
                return true;
              },
            },
          },
        }),
        logGroup: new logs.LogGroup(this, 'FeedbackAnalystFnLogs', {
          retention: GlobalConfig.logRetentionDays,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
        environment: {
          LOG_LEVEL: LambdaConfig.logLevel,
          RESULTS_TABLE: props.resultsTable.tableName,
          INVENTORY_BUCKET: props.inventoryBucket?.bucketName ?? '',
          MEMORY_ID: props.memoryId ?? '',
          PROMPTS_BUCKET: props.promptsBucket?.bucketName ?? '',
          PROMPTS_KEY: 'config/prompts.json',
        },
        timeout: cdk.Duration.seconds(60),
        memorySize: 256,
      });

      // DynamoDB Stream event source — filter for INSERT and MODIFY events only.
      // The Lambda handler itself filters for FEEDBACK# sort key prefix.
      feedbackAnalystFunction.addEventSource(
        new DynamoEventSource(props.preferencesTable, {
          startingPosition: lambda.StartingPosition.LATEST,
          batchSize: 1,
          retryAttempts: 2,
          filters: [
            lambda.FilterCriteria.filter({
              eventName: lambda.FilterRule.isEqual('INSERT'),
            }),
            lambda.FilterCriteria.filter({
              eventName: lambda.FilterRule.isEqual('MODIFY'),
            }),
          ],
        }),
      );

      // Grant read access to results table, inventory bucket, and preferences table stream
      props.resultsTable.grantReadData(feedbackAnalystFunction);
      if (props.inventoryBucket) {
        props.inventoryBucket.grantRead(feedbackAnalystFunction);
      }
      props.preferencesTable.grantStreamRead(feedbackAnalystFunction);

      // Grant Bedrock model invocation permissions
      feedbackAnalystFunction.addToRolePolicy(new iam.PolicyStatement({
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: [
          `arn:aws:bedrock:${this.region}::foundation-model/*`,
          `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/*`,
        ],
      }));

      // Grant S3 read access for prompts
      if (props.promptsBucket) {
        props.promptsBucket.grantRead(feedbackAnalystFunction, 'config/*');
      }

      // Grant AgentCore Memory operations for the Strands SDK session manager.
      const memoryArnPattern = props.memoryId
        ? `arn:aws:bedrock-agentcore:${this.region}:${this.account}:memory/${props.memoryId}`
        : `arn:aws:bedrock-agentcore:${this.region}:${this.account}:memory/${FeedbackConfig.memoryName}-*`;
      feedbackAnalystFunction.addToRolePolicy(new iam.PolicyStatement({
        actions: [
          'bedrock-agentcore:CreateEvent',
          'bedrock-agentcore:ListEvents',
          'bedrock-agentcore:RetrieveMemoryRecords',
          'bedrock-agentcore:ListMemoryRecords',
          'bedrock-agentcore:ListSessions',
          'bedrock-agentcore:ListActors',
          'bedrock-agentcore:GetMemory',
        ],
        resources: [memoryArnPattern],
      }));

      // Feedback Analyst error alarm
      if (props.alertsTopic) {
        const analystAlarm = new cloudwatch.Alarm(this, 'FeedbackAnalystErrorAlarm', {
          metric: feedbackAnalystFunction.metricErrors({ period: cdk.Duration.minutes(5) }),
          threshold: 1,
          evaluationPeriods: 1,
          alarmDescription: `${GlobalConfig.deploymentPrefix} Feedback Analyst Lambda error rate exceeded threshold`,
          treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
          comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        });
        analystAlarm.addAlarmAction(new cw_actions.SnsAction(props.alertsTopic));
      }

    // ─── Add API Gateway as /api/* origin on CloudFront ───
    distribution.addBehavior('/api/*', new origins.RestApiOrigin(api), {
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
      cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
      originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
    });

    // ─── Custom Resource: Update CORS origins post-deployment ───
    const corsUpdaterFunction = new lambda.Function(this, 'CorsUpdaterFunction', {
      description: 'Updates API Gateway CORS origins with CloudFront distribution domain',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.handler',
      code: lambda.Code.fromAsset('src/frontend/cors-updater'),
      logGroup: new logs.LogGroup(this, 'CorsUpdaterFnLogs', {
        retention: GlobalConfig.logRetentionDays,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      timeout: cdk.Duration.seconds(60),
    });

    corsUpdaterFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['apigateway:GET', 'apigateway:PATCH'],
      resources: [
        `arn:aws:apigateway:${this.region}::/restapis/${api.restApiId}/*`,
      ],
    }));

    corsUpdaterFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['apigateway:POST'],
      resources: [
        `arn:aws:apigateway:${this.region}::/restapis/${api.restApiId}/deployments`,
      ],
    }));

    const corsUpdaterProvider = new cr.Provider(this, 'CorsUpdaterProvider', {
      onEventHandler: corsUpdaterFunction,
    });

    const corsUpdaterResource = new cdk.CustomResource(this, 'CorsUpdaterResource', {
      serviceToken: corsUpdaterProvider.serviceToken,
      properties: {
        RestApiId: api.restApiId,
        StageName: 'prod',
        DistributionDomain: `https://${distribution.distributionDomainName}`,
        // Force update on every deployment so CORS is always re-applied
        DeploymentTimestamp: Date.now().toString(),
      },
    });

    // Ensure the custom resource runs AFTER the API Gateway deployment stage is created,
    // so our CORS update doesn't get overwritten by CDK's deployment.
    corsUpdaterResource.node.addDependency(api.deploymentStage);

    // ─── Outputs ───
    new cdk.CfnOutput(this, 'CloudFrontURL', {
      value: `https://${distribution.distributionDomainName}`,
    });

    new cdk.CfnOutput(this, 'CloudFrontDistributionId', {
      value: distribution.distributionId,
    });

    new cdk.CfnOutput(this, 'WebsiteBucketName', {
      value: websiteBucket.bucketName,
    });

    new cdk.CfnOutput(this, 'EdgeAuthFunctionName', {
      value: edgeAuthFunction.functionName,
    });

    new cdk.CfnOutput(this, 'UserPoolId', {
      value: userPool.userPoolId,
    });

    new cdk.CfnOutput(this, 'UserPoolClientId', {
      value: cfnUserPoolClient.ref,
    });

    new cdk.CfnOutput(this, 'CognitoDomainURL', {
      value: cognitoDomain.baseUrl(),
    });

    new cdk.CfnOutput(this, 'CognitoLoginURL', {
      value: `${cognitoDomain.baseUrl()}/login?client_id=${cfnUserPoolClient.ref}&response_type=code&scope=openid+email+profile&redirect_uri=${callbackUrl}`,
    });

    new cdk.CfnOutput(this, 'ApiGatewayURL', {
      value: api.url,
    });

    // ─── Lambda error alarms → SNS alerts ───

    if (props.alertsTopic) {
      const lambdasToMonitor: { fn: lambda.Function | cloudfront.experimental.EdgeFunction; name: string }[] = [
        { fn: preferencesFunction, name: 'Preferences' },
        { fn: resultsFunction, name: 'Results' },
        { fn: feedbackFunction, name: 'Feedback' },
        { fn: accountsFunction, name: 'Accounts' },
      ];

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
      }

      // ─── API Gateway 5xx alarm ───

      const api5xxAlarm = new cloudwatch.Alarm(this, 'Api5xxAlarm', {
        metric: api.metricServerError({ period: cdk.Duration.minutes(5) }),
        threshold: 5,
        evaluationPeriods: 1,
        alarmDescription: `${GlobalConfig.deploymentPrefix} API Gateway 5xx error rate exceeded threshold`,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      });
      api5xxAlarm.addAlarmAction(new cw_actions.SnsAction(props.alertsTopic));

      // ─── API Gateway latency alarm ───

      const apiLatencyAlarm = new cloudwatch.Alarm(this, 'ApiLatencyAlarm', {
        metric: api.metricLatency({
          period: cdk.Duration.minutes(5),
          statistic: 'p95',
        }),
        threshold: 5000, // 5 seconds p95
        evaluationPeriods: 2,
        alarmDescription: `${GlobalConfig.deploymentPrefix} API Gateway p95 latency exceeded 5 seconds`,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      });
      apiLatencyAlarm.addAlarmAction(new cw_actions.SnsAction(props.alertsTopic));
    }

    suppressFrontendFindings(this);
  }
}
