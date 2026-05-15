// ─────────────────────────────────────────────────────────────────────────────
// SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
// This code is provided as a reference implementation only.
// ─────────────────────────────────────────────────────────────────────────────

import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';

interface LogRetentionSetterProps {
  /**
   * Log group name prefix to match, e.g. "/aws/bedrock-agentcore/runtimes/my_prefix"
   */
  logGroupPrefix: string;
  /** Retention in days (default: 30) */
  retentionDays?: number;
}

/**
 * Custom resource that finds CloudWatch log groups by prefix and sets
 * their retention policy. Useful for log groups created by services
 * (like AgentCore) where the exact name isn't known at deploy time.
 */
export class LogRetentionSetter extends Construct {
  constructor(scope: Construct, id: string, props: LogRetentionSetterProps) {
    super(scope, id);

    const retentionDays = props.retentionDays ?? 30;

    const fn = new lambda.Function(this, 'Handler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.handler',
      logGroup: new logs.LogGroup(this, 'HandlerLogs', {
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      timeout: cdk.Duration.seconds(60),
      code: lambda.Code.fromInline(`
import boto3
import cfnresponse
import os
import logging

log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(level=getattr(logging, log_level, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def handler(event, context):
    try:
        if event['RequestType'] == 'Delete':
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
            return

        prefix = event['ResourceProperties']['LogGroupPrefix']
        days = int(event['ResourceProperties']['RetentionDays'])
        region = os.environ.get('AWS_REGION', 'eu-west-1')

        client = boto3.client('logs', region_name=region)
        paginator = client.get_paginator('describe_log_groups')
        updated = []

        for page in paginator.paginate(logGroupNamePrefix=prefix):
            for lg in page.get('logGroups', []):
                name = lg['logGroupName']
                current = lg.get('retentionInDays')
                if current != days:
                    client.put_retention_policy(logGroupName=name, retentionInDays=days)
                    logger.info(f"Set retention on {name}: {current} -> {days}d")
                    updated.append(name)
                else:
                    logger.info(f"Retention already {days}d on {name}")

        cfnresponse.send(event, context, cfnresponse.SUCCESS, {
            'UpdatedLogGroups': ','.join(updated) if updated else 'none',
        })
    except Exception as e:
        logger.error(f"Failed: {e}", exc_info=True)
        cfnresponse.send(event, context, cfnresponse.FAILED, {'Error': str(e)})
`),
      environment: { LOG_LEVEL: 'INFO' },
    });

    fn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['logs:DescribeLogGroups', 'logs:PutRetentionPolicy'],
      resources: ['*'],
    }));

    const provider = new cr.Provider(this, 'Provider', { onEventHandler: fn });

    new cdk.CustomResource(this, 'Resource', {
      serviceToken: provider.serviceToken,
      properties: {
        LogGroupPrefix: props.logGroupPrefix,
        RetentionDays: retentionDays.toString(),
        // Force re-run on every deploy so new log groups get picked up
        Timestamp: Date.now().toString(),
      },
    });
  }
}
