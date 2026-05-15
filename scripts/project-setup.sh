#!/usr/bin/env bash
#
# AWANA Project Setup
#
# Deploys the foundation stack and prepares the environment for
# deploying all remaining stacks.
#
# Usage:
#   ./scripts/project-setup.sh
#
set -euo pipefail

echo "── AWANA Project Setup ──"
echo ""

# ── 0. Precondition checks ──

echo "Checking prerequisites..."
MISSING=()

# AWS CLI
if ! command -v aws &>/dev/null; then
  MISSING+=("  ✗ aws CLI — install from https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html")
else
  echo "  ✓ aws CLI"
fi

# Node.js
if ! command -v node &>/dev/null; then
  MISSING+=("  ✗ node — install Node.js 22+ from https://nodejs.org/")
else
  echo "  ✓ node $(node --version)"
fi

# npm
if ! command -v npm &>/dev/null; then
  MISSING+=("  ✗ npm — included with Node.js, check your installation")
else
  echo "  ✓ npm $(npm --version)"
fi

# Docker (needed for agent code bundling)
if ! command -v docker &>/dev/null; then
  MISSING+=("  ✗ docker — install from https://docs.docker.com/get-docker/")
else
  echo "  ✓ docker $(docker --version | awk '{print $3}' | tr -d ',')"
fi

# AWS credentials
if ! aws sts get-caller-identity &>/dev/null; then
  MISSING+=("  ✗ AWS credentials — run 'aws configure' or set AWS_PROFILE")
else
  ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
  echo "  ✓ AWS credentials (account ${ACCOUNT_ID})"
fi

# Cost Explorer
if aws sts get-caller-identity &>/dev/null; then
  CE_CHECK=$(aws ce get-cost-and-usage \
    --time-period Start="$(date -u -v-1d +%Y-%m-%d 2>/dev/null || date -u -d '1 day ago' +%Y-%m-%d)",End="$(date -u +%Y-%m-%d)" \
    --granularity DAILY \
    --metrics BlendedCost \
    --query 'ResultsByTime[0].TimePeriod.Start' \
    --output text 2>/dev/null || echo "ERROR")
  if [[ "${CE_CHECK}" == "ERROR" ]]; then
    MISSING+=("  ✗ Cost Explorer — enable it at https://console.aws.amazon.com/cost-management/home#/cost-explorer (takes ~24h to activate)")
  else
    echo "  ✓ Cost Explorer enabled"
  fi
fi

if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo ""
  echo "Missing prerequisites:"
  for msg in "${MISSING[@]}"; do
    echo "${msg}"
  done
  echo ""
  echo "Setup cancelled. Install the missing tools and try again."
  exit 1
fi

echo ""

# ── 1. Prompt for deployment region ──
read -rp "Enter the AWS region to deploy AWANA [eu-west-1]: " REGION
REGION="${REGION:-eu-west-1}"
echo "  Deployment region: ${REGION}"
echo ""

# ── 2. Prompt for Resource Explorer view ARN ──
echo "AWANA needs a Resource Explorer view to discover resources."
echo "This can be an org-scoped view (multi-account) or a local view (single account)."
echo ""
read -rp "Enter the Resource Explorer view ARN: " RE_VIEW_ARN

# Validate ARN format: arn:aws:resource-explorer-2:<region>:<account>:view/<name>/<id>
if [[ ! "${RE_VIEW_ARN}" =~ ^arn:aws:resource-explorer-2:[a-z0-9-]+:[0-9]{12}:view/.+/.+$ ]]; then
  echo "✗ Invalid Resource Explorer view ARN format."
  echo "  Expected: arn:aws:resource-explorer-2:<region>:<account>:view/<name>/<id>"
  echo "  Example:  arn:aws:resource-explorer-2:eu-west-1:123456789012:view/my-view/abcd1234-..."
  exit 1
fi

# Derive the aggregator region from the ARN (4th colon-separated field)
AGGREGATOR_REGION=$(echo "${RE_VIEW_ARN}" | cut -d: -f4)
echo "  View ARN: ${RE_VIEW_ARN}"
echo "  Derived aggregator region: ${AGGREGATOR_REGION}"
if [[ "${AGGREGATOR_REGION}" != "${REGION}" ]]; then
  echo "  ⚠ View is in a different region than deployment — cross-region calls will be used."
fi
echo ""

# ── 3. Optional: alert email ──
read -rp "Enter an email address for alerts (failures, circuit breaker) or press Enter to skip: " ALERT_EMAIL
if [[ -n "${ALERT_EMAIL}" ]]; then
  echo "  Alert email: ${ALERT_EMAIL} (subscription will be created after deploy)"
fi
echo ""

# ── 4. Verify the Resource Explorer view exists ──
echo "Verifying Resource Explorer view..."
VIEW_CHECK=$(aws resource-explorer-2 get-view \
  --view-arn "${RE_VIEW_ARN}" \
  --region "${AGGREGATOR_REGION}" \
  --query 'View.ViewArn' \
  --output text 2>/dev/null || echo "ERROR")

if [[ "${VIEW_CHECK}" == "ERROR" ]]; then
  echo "✗ Could not find the Resource Explorer view."
  echo "  ARN: ${RE_VIEW_ARN}"
  echo ""
  echo "  Make sure:"
  echo "    1. Resource Explorer is enabled in ${AGGREGATOR_REGION}"
  echo "    2. The view exists (org-scoped for multi-account, or local for single-account)"
  echo "    3. Your credentials have resource-explorer-2:GetView permission"
  echo ""
  echo "  Docs: https://docs.aws.amazon.com/resource-explorer/latest/userguide/getting-started-setting-up.html"
  exit 1
fi

echo "✓ Resource Explorer view confirmed: ${VIEW_CHECK}"
echo ""

# ── 5. Install dependencies ──
echo "Installing npm dependencies..."
npm install

# ── 5b. Prompt for deployment prefix ──
read -rp "Enter a deployment prefix (allows multiple deployments in the same account) [AWANA]: " DEPLOY_PREFIX
DEPLOY_PREFIX="${DEPLOY_PREFIX:-AWANA}"
echo "  Deployment prefix: ${DEPLOY_PREFIX}"
echo ""

# Capitalize first letter for stack name prefix
STACK_PREFIX="$(echo "${DEPLOY_PREFIX:0:1}" | tr '[:lower:]' '[:upper:]')${DEPLOY_PREFIX:1}"
FOUNDATION_STACK="${STACK_PREFIX}FoundationStack"

# ── 6. Write context to cdk.context.json for subsequent deploys and delete-app.sh ──
# All three values are persisted so that `cdk deploy --all` works without
# passing -c flags after initial setup. cdk.context.json is gitignored.
echo "Saving deployment context to cdk.context.json..."

node -e "
  const fs = require('fs');
  const ctxFile = 'cdk.context.json';
  let ctx = {};
  try { ctx = JSON.parse(fs.readFileSync(ctxFile, 'utf-8')); } catch {}
  ctx['awana:deploymentPrefix'] = '${DEPLOY_PREFIX}';
  ctx['awana:deploymentRegion'] = '${REGION}';
  ctx['awana:resourceExplorerViewArn'] = '${RE_VIEW_ARN}';
  fs.writeFileSync(ctxFile, JSON.stringify(ctx, null, 2) + '\n');
"
echo ""

# ── 7. CDK Bootstrap ──
# AWANA uses Lambda@Edge (CloudFront) which requires assets to be published to
# us-east-1 regardless of the deployment region, so we bootstrap both.
read -rp "Do you need to bootstrap CDK in this account? [y/N] " bootstrap
if [[ "${bootstrap}" =~ ^[Yy]$ ]]; then
  ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

  echo "Bootstrapping CDK in ${REGION}..."
  npx cdk bootstrap "aws://${ACCOUNT_ID}/${REGION}"

  if [[ "${REGION}" != "us-east-1" ]]; then
    echo "Bootstrapping CDK in us-east-1 (required for Lambda@Edge)..."
    npx cdk bootstrap "aws://${ACCOUNT_ID}/us-east-1"
  else
    echo "Deployment region is us-east-1 — no separate Lambda@Edge bootstrap needed."
  fi
fi

# ── 8. Deploy all stacks ──
echo ""
echo "Deploying all stacks..."
npx cdk deploy --all \
  -c region="${REGION}" \
  -c prefix="${DEPLOY_PREFIX}" \
  -c resourceExplorerViewArn="${RE_VIEW_ARN}" \
  --require-approval never

# ── 9. Subscribe alert email to SNS topic (if provided) ──
if [[ -n "${ALERT_EMAIL:-}" ]]; then
  ALERTS_TOPIC_ARN=$(aws cloudformation describe-stacks \
    --stack-name "${FOUNDATION_STACK}" \
    --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='AlertsTopicArn'].OutputValue" \
    --output text 2>/dev/null || echo "")

  if [[ -n "${ALERTS_TOPIC_ARN}" && "${ALERTS_TOPIC_ARN}" != "None" ]]; then
    echo "Subscribing ${ALERT_EMAIL} to alerts topic..."
    aws sns subscribe \
      --topic-arn "${ALERTS_TOPIC_ARN}" \
      --protocol email \
      --notification-endpoint "${ALERT_EMAIL}" \
      --region "${REGION}"
    echo "✓ Subscription created. Check your inbox to confirm the subscription."
  else
    echo "⚠ Could not find AlertsTopicArn in stack outputs — skipping email subscription."
  fi
  echo ""
fi

# ── 10. Create initial Cognito user (optional) ──
echo ""
read -rp "Enter an email address to create the first Cognito user (or press Enter to skip): " COGNITO_EMAIL

if [[ -n "${COGNITO_EMAIL}" ]]; then
  read -rsp "Enter a temporary password (min 8 chars, must include uppercase, lowercase, number, symbol): " COGNITO_PASSWORD
  echo ""

  FRONTEND_STACK="${STACK_PREFIX}FrontendStack"
  USER_POOL_ID=$(aws cloudformation describe-stacks \
    --stack-name "${FRONTEND_STACK}" \
    --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" \
    --output text 2>/dev/null || echo "")

  if [[ -z "${USER_POOL_ID}" || "${USER_POOL_ID}" == "None" ]]; then
    echo "⚠ Could not find UserPoolId in ${FRONTEND_STACK} outputs — skipping user creation."
  else
    aws cognito-idp admin-create-user \
      --user-pool-id "${USER_POOL_ID}" \
      --username "${COGNITO_EMAIL}" \
      --user-attributes Name=email,Value="${COGNITO_EMAIL}" Name=email_verified,Value=true \
      --temporary-password "${COGNITO_PASSWORD}" \
      --region "${REGION}"
    echo "✓ User ${COGNITO_EMAIL} created. You'll be prompted to set a permanent password on first login."
  fi
fi

# ── 11. Done ──
echo ""
echo "✓ Project setup complete."

FRONTEND_STACK="${STACK_PREFIX}FrontendStack"
CLOUDFRONT_URL=$(aws cloudformation describe-stacks \
  --stack-name "${FRONTEND_STACK}" \
  --region "${REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontURL'].OutputValue" \
  --output text 2>/dev/null || echo "")

if [[ -n "${CLOUDFRONT_URL}" && "${CLOUDFRONT_URL}" != "None" ]]; then
  echo ""
  echo "  Frontend: ${CLOUDFRONT_URL}"
fi
