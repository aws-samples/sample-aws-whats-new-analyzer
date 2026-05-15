#!/usr/bin/env node
// ─────────────────────────────────────────────────────────────────────────────
// SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
// This code is provided as a reference implementation only.
// ─────────────────────────────────────────────────────────────────────────────

import * as cdk from 'aws-cdk-lib';
import { NagSuppressions } from 'cdk-nag';
import { execSync } from 'child_process';
import { FoundationStack } from '../lib/foundation-stack';
import { IngestionStack } from '../lib/ingestion-stack';
import { EvaluationStack } from '../lib/evaluation-stack';
import { FrontendStack } from '../lib/frontend-stack';
import { GlobalConfig } from '../config';

// ── Git metadata (resolved at synth time) ──

function git(cmd: string): string {
  try {
    return execSync(`git ${cmd}`, { encoding: 'utf-8' }).trim();
  } catch {
    return 'unknown';
  }
}

const gitTags: Record<string, string> = {
  'git:repo': git('remote get-url origin'),
  'git:commit': git('rev-parse HEAD'),
  'git:branch': git('rev-parse --abbrev-ref HEAD'),
};

// ── App ──

const app = new cdk.App();

// ── Resolve deployment config from CDK context (-c flags) ──
// Context can come from: -c flags on CLI, cdk.json context, or cdk.context.json.
// The setup script writes awana:deploymentPrefix and awana:deploymentRegion to
// cdk.json so that subsequent deploys and the delete script can find them.

GlobalConfig.deploymentRegion = app.node.tryGetContext('region')
  || app.node.tryGetContext('awana:deploymentRegion') || '';
GlobalConfig.deploymentPrefix = app.node.tryGetContext('prefix')
  || app.node.tryGetContext('awana:deploymentPrefix') || '';
GlobalConfig.resourceExplorerViewArn = app.node.tryGetContext('resourceExplorerViewArn')
  || app.node.tryGetContext('awana:resourceExplorerViewArn') || '';

const deploymentRegion = GlobalConfig.deploymentRegion;
const deploymentPrefix = GlobalConfig.deploymentPrefix;
const resourceExplorerViewArn = GlobalConfig.resourceExplorerViewArn;

if (!deploymentRegion) {
  throw new Error(
    'Missing context value "region". ' +
    'Pass it via: cdk deploy -c region=eu-west-1 -c prefix=AWANA -c resourceExplorerViewArn=arn:...',
  );
}

if (!deploymentPrefix) {
  throw new Error(
    'Missing context value "prefix". ' +
    'Pass it via: cdk deploy -c region=eu-west-1 -c prefix=AWANA -c resourceExplorerViewArn=arn:...',
  );
}

const env = {
  region: deploymentRegion,
  account: process.env.CDK_DEFAULT_ACCOUNT,
};

// Apply git tags to every stack
for (const [key, value] of Object.entries(gitTags)) {
  if (value && value !== 'unknown') {
    cdk.Tags.of(app).add(key, value);
  }
}
cdk.Tags.of(app).add('project', GlobalConfig.deploymentPrefix);

const stackPrefix = GlobalConfig.deploymentPrefix.charAt(0).toUpperCase() + GlobalConfig.deploymentPrefix.slice(1);

// ── Stacks ──

const foundation = new FoundationStack(app, `${stackPrefix}FoundationStack`, { env });

const ingestionStack = new IngestionStack(app, `${stackPrefix}IngestionStack`, {
  env,
  preferencesTable: foundation.preferencesTable,
  inventoryBucket: foundation.inventoryBucket,
  alertsTopic: foundation.alertsTopic,
  resourceExplorerViewArn,
  boto3Layer: foundation.boto3Layer,
});

const evaluationStack = new EvaluationStack(app, `${stackPrefix}EvaluationStack`, {
  env,
  preferencesTable: foundation.preferencesTable,
  resultsTable: foundation.resultsTable,
  inventoryBucket: foundation.inventoryBucket,
  promptsBucket: foundation.promptsBucket,
  announcementsQueue: ingestionStack.announcementsQueue,
  alertsTopic: foundation.alertsTopic,
  boto3Layer: foundation.boto3Layer,
});

const frontendStack = new FrontendStack(app, `${stackPrefix}FrontendStack`, {
  env,
  preferencesTable: foundation.preferencesTable,
  resultsTable: foundation.resultsTable,
  inventoryBucket: foundation.inventoryBucket,
  promptsBucket: foundation.promptsBucket,
  memoryId: evaluationStack.memoryId,
  alertsTopic: foundation.alertsTopic,
  boto3Layer: foundation.boto3Layer,
});

// ── Ensure downstream stacks deploy after the foundation (boundary must exist first) ──
ingestionStack.addDependency(foundation, 'Permissions boundary must exist before roles are created');
evaluationStack.addDependency(foundation, 'Permissions boundary must exist before roles are created');
frontendStack.addDependency(foundation, 'Permissions boundary must exist before roles are created');

// ── Edge Lambda suppression + dependency ──
// cloudfront.experimental.EdgeFunction creates a separate top-level stack
// that can't be suppressed from within the FrontendStack.
// It also needs an explicit dependency on the foundation stack so the
// permissions boundary policy exists before its IAM role is created.
for (const child of app.node.children) {
  if (child.node.id.startsWith('edge-lambda-stack-')) {
    const edgeStack = child as cdk.Stack;
    edgeStack.addDependency(foundation, 'Permissions boundary must exist before roles are created');
    NagSuppressions.addStackSuppressions(edgeStack, [
      { id: 'AwsSolutions-L1', reason: 'Lambda@Edge runtime pinned to Python 3.12 — not all runtimes are supported at edge' },
      { id: 'AwsSolutions-IAM4', reason: 'Lambda@Edge uses AWSLambdaBasicExecutionRole — required by the service' },
    ]);
  }
}
