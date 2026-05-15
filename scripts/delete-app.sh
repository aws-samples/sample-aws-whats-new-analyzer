#!/usr/bin/env bash
#
# AWANA Delete App — Reliable Teardown
#
# Safely tears down the entire AWANA deployment. Because Lambda@Edge
# replicas take time to be removed by CloudFront, this script works
# in two phases:
#
#   Phase 1 (first run):
#     1. Disables the CloudFront distribution.
#     2. Verifies the distribution config is truly disabled and waits
#        for it to reach "Deployed" status.
#     3. Removes Lambda@Edge associations from the (now disabled)
#        distribution and waits for "Deployed" again.
#     4. Deletes Frontend, Evaluation, and Ingestion stacks.
#        FoundationStack is NOT deleted here because it shares
#        dependencies with the edge-lambda stack (e.g. IAM roles,
#        log groups) and must be torn down together in Phase 2.
#
#   Phase 2 (second run, after ~30-60 min):
#     1. Deletes the edge-lambda stack and FoundationStack.
#        These two depend on each other and must be deleted together
#        after Lambda@Edge replicas have fully drained.
#
# Usage:
#   ./scripts/delete-app.sh
#
set -euo pipefail

# ── Parse cdk.context.json once ──
_CDK_OUTPUT=$(node -e "
  const fs=require('fs');
  let c={};
  try { c=JSON.parse(fs.readFileSync('cdk.context.json','utf-8')); } catch {}
  process.stdout.write((c['awana:deploymentPrefix']||'')+' '+(c['awana:deploymentRegion']||''));
" 2>/dev/null || echo " ")
read -r PREFIX REGION <<< "${_CDK_OUTPUT}"

if [[ -z "${REGION}" ]]; then
  echo "✗ Could not determine deployment region from cdk.context.json."
  echo "  Run ./scripts/project-setup.sh first, or create cdk.context.json with:"
  echo '  { "awana:deploymentPrefix": "AWANA", "awana:deploymentRegion": "eu-west-1" }'
  exit 1
fi

if [[ -z "${PREFIX}" ]]; then
  echo "✗ Could not determine deployment prefix from cdk.context.json."
  exit 1
fi

STACK_PREFIX="$(tr '[:lower:]' '[:upper:]' <<< "${PREFIX:0:1}")${PREFIX:1}"
FRONTEND_STACK="${STACK_PREFIX}FrontendStack"
FOUNDATION_STACK="${STACK_PREFIX}FoundationStack"
INGESTION_STACK="${STACK_PREFIX}IngestionStack"
EVALUATION_STACK="${STACK_PREFIX}EvaluationStack"
STATE_FILE=".delete-app-state"

echo "Deployment: ${PREFIX} | Region: ${REGION}"

# ── Temp file management ──
TMPFILE=$(mktemp)
trap 'rm -f "${TMPFILE}" "${TMPFILE}.updated"' EXIT

# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════

get_distribution_id() {
  aws cloudformation describe-stacks \
    --stack-name "${FRONTEND_STACK}" \
    --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDistributionId'].OutputValue" \
    --output text 2>/dev/null || true
}

get_edge_lambda_stack() {
  aws cloudformation list-stacks \
    --region us-east-1 \
    --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
    --query "StackSummaries[?starts_with(StackName,'edge-lambda-stack-')].StackName" \
    --output text 2>/dev/null || true
}

# Wait for a CloudFront distribution to reach a target status.
# Args: $1=dist_id  $2=target_status  $3=max_attempts (default 30, ~10 min)
wait_for_dist() {
  local dist_id="$1" target="$2" max="${3:-30}" i=0 status
  while (( i < max )); do
    status=$(aws cloudfront get-distribution --id "${dist_id}" \
      --query "Distribution.Status" --output text 2>/dev/null || echo "UNKNOWN")
    [[ "${status}" == "${target}" ]] && return 0
    i=$((i + 1))
    printf "  Status: %-12s (attempt %d/%d)\r" "${status}" "${i}" "${max}"
    sleep 20
  done
  echo ""
  return 1
}

# Delete a CloudFormation stack and wait for completion.
# Args: $1=stack_name  $2=region
delete_stack() {
  local stack="$1" region="$2" status
  status=$(aws cloudformation describe-stacks \
    --stack-name "${stack}" --region "${region}" \
    --query "Stacks[0].StackStatus" --output text 2>/dev/null || echo "GONE")

  if [[ "${status}" == "GONE" ]]; then
    echo "  ⏭  ${stack} — does not exist, skipping."
    return 0
  fi

  echo "  ⏳ ${stack} — deleting..."
  aws cloudformation delete-stack --stack-name "${stack}" --region "${region}"

  if ! aws cloudformation wait stack-delete-complete \
    --stack-name "${stack}" --region "${region}" 2>/dev/null; then
    status=$(aws cloudformation describe-stacks \
      --stack-name "${stack}" --region "${region}" \
      --query "Stacks[0].StackStatus" --output text 2>/dev/null || echo "GONE")
    if [[ "${status}" == "DELETE_FAILED" ]]; then
      echo "  ✗ ${stack} — deletion failed. Check the CloudFormation console."
      return 1
    fi
  fi
  echo "  ✓ ${stack} — deleted."
}

# ══════════════════════════════════════════════════════════════════════
# Phase 2: state file exists → finish by deleting the edge stack
# ══════════════════════════════════════════════════════════════════════
if [[ -f "${STATE_FILE}" ]]; then
  PHASE1_TIME=$(grep 'phase1_complete=' "${STATE_FILE}" | cut -d= -f2)
  EDGE_STACK=$(grep 'edge_stack=' "${STATE_FILE}" | cut -d= -f2 || true)

  echo ""
  echo "── Phase 2: Deleting the edge-lambda and Foundation stacks ──"
  echo "   Phase 1 completed at: ${PHASE1_TIME}"
  echo ""
  echo "⚠  Lambda@Edge replicas need 30-60 min to drain after disassociation."
  echo "   Proceeding too early will fail with: 'Cannot delete function - replicas still exist'"
  echo ""
  read -rp "Ready to proceed? [y/N] " confirm
  [[ "${confirm}" =~ ^[Yy]$ ]] || { echo "Re-run later: ./scripts/delete-app.sh"; exit 0; }

  [[ -z "${EDGE_STACK}" ]] && EDGE_STACK=$(get_edge_lambda_stack)

  # FoundationStack and edge-lambda share dependencies (IAM roles, log groups),
  # so they must be deleted together. Delete edge-lambda first, then Foundation.
  if [[ -z "${EDGE_STACK}" ]]; then
    echo "✓ No edge-lambda stack found — already cleaned up."
  else
    delete_stack "${EDGE_STACK}" "us-east-1" || {
      echo ""
      echo "  Replicas may still be draining. Wait a bit longer and re-run."
      exit 1
    }
  fi

  delete_stack "${FOUNDATION_STACK}" "${REGION}" || {
    echo ""
    echo "  FoundationStack deletion failed. Check the CloudFormation console."
    exit 1
  }

  rm -f "${STATE_FILE}"
  echo ""
  echo "✓ All ${PREFIX} stacks destroyed. Cleanup complete."
  exit 0
fi

# ══════════════════════════════════════════════════════════════════════
# Phase 1: Disable + strip edge lambda, verify, delete stacks
# ══════════════════════════════════════════════════════════════════════

echo ""
echo "⚠  This will tear down the ENTIRE ${PREFIX} deployment in ${REGION}."
echo "   This action cannot be undone."
echo ""
read -rp "Proceed? [y/N] " confirm
[[ "${confirm}" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

DIST_ID=$(get_distribution_id)

# If the distribution or frontend stack is already gone, skip CloudFront steps
if [[ -z "${DIST_ID}" || "${DIST_ID}" == "None" ]]; then
  echo ""
  echo "── Phase 1: Preparing for deletion ──"
  echo "   CloudFront distribution not found — skipping Steps 1-3."
  echo ""
else
  echo ""
  echo "── Phase 1: Preparing for deletion ──"
  echo "   Distribution: ${DIST_ID}"
  echo ""

  # ── Step 1: Disable the distribution ──
  echo "Step 1/4: Disabling CloudFront distribution..."

  ALREADY_DISABLED=$(aws cloudfront get-distribution-config --id "${DIST_ID}" \
    --query "DistributionConfig.Enabled" --output text 2>/dev/null || echo "UNKNOWN")

  if [[ "${ALREADY_DISABLED}" == "False" ]]; then
    echo "  ⏭  Distribution already disabled, skipping."
  else
    aws cloudfront get-distribution-config --id "${DIST_ID}" --output json > "${TMPFILE}"

    ETAG=$(node -p "JSON.parse(require('fs').readFileSync('${TMPFILE}','utf-8')).ETag")

    node -e "
      const data = JSON.parse(require('fs').readFileSync('${TMPFILE}', 'utf-8'));
      const cfg = data.DistributionConfig;
      cfg.Enabled = false;
      require('fs').writeFileSync('${TMPFILE}.updated', JSON.stringify(cfg, null, 2));
    "

    aws cloudfront update-distribution \
      --id "${DIST_ID}" \
      --if-match "${ETAG}" \
      --distribution-config "file://${TMPFILE}.updated" \
      --output json > "${TMPFILE}.response"

    echo "  Update submitted."
  fi

  # ── Step 2: Verify disabled + wait for Deployed status ──
  echo ""
  echo "Step 2/4: Verifying distribution is disabled..."

  ENABLED=$(aws cloudfront get-distribution-config --id "${DIST_ID}" \
    --query "DistributionConfig.Enabled" --output text 2>/dev/null || echo "UNKNOWN")

  if [[ "${ENABLED}" != "False" ]]; then
    echo "  ✗ SECURITY CHECK FAILED: Distribution config still shows Enabled=${ENABLED}"
    echo "    Cannot safely proceed. Check the AWS console and retry."
    exit 1
  fi
  echo "  ✓ Config confirmed: Enabled=False."

  echo "  Waiting for distribution to reach 'Deployed' status..."
  if wait_for_dist "${DIST_ID}" "Deployed" 30; then
    echo ""
    echo "  ✓ Distribution fully deployed in disabled state."
  else
    echo "  ⚠ Timed out waiting for 'Deployed' — proceeding anyway (config is confirmed disabled)."
  fi

  # ── Step 3: Remove Lambda@Edge associations ──
  echo ""
  echo "Step 3/4: Removing Lambda@Edge associations..."

  # Check if there are any Lambda@Edge associations to remove
  aws cloudfront get-distribution-config --id "${DIST_ID}" --output json > "${TMPFILE}"

  HAS_ASSOCIATIONS=$(node -p "
    const cfg = JSON.parse(require('fs').readFileSync('${TMPFILE}','utf-8')).DistributionConfig;
    const count = (cfg.DefaultCacheBehavior?.LambdaFunctionAssociations?.Quantity || 0)
      + (cfg.CacheBehaviors?.Items || []).reduce((s,b) => s + (b.LambdaFunctionAssociations?.Quantity || 0), 0);
    count > 0;
  " 2>/dev/null || echo "false")

  if [[ "${HAS_ASSOCIATIONS}" == "false" ]]; then
    echo "  ⏭  No Lambda@Edge associations found, skipping."
  else
    # ${TMPFILE} already has the latest config from the check above
    ETAG=$(node -p "JSON.parse(require('fs').readFileSync('${TMPFILE}','utf-8')).ETag")

    node -e "
      const data = JSON.parse(require('fs').readFileSync('${TMPFILE}', 'utf-8'));
      const cfg = data.DistributionConfig;
      const strip = b => { b.LambdaFunctionAssociations = { Quantity: 0, Items: [] }; };
      if (cfg.DefaultCacheBehavior) strip(cfg.DefaultCacheBehavior);
      (cfg.CacheBehaviors?.Items || []).forEach(strip);
      require('fs').writeFileSync('${TMPFILE}.updated', JSON.stringify(cfg, null, 2));
    "

    aws cloudfront update-distribution \
      --id "${DIST_ID}" \
      --if-match "${ETAG}" \
      --distribution-config "file://${TMPFILE}.updated" \
      --output json > "${TMPFILE}.response"

    echo "  ✓ Lambda@Edge associations removed."

    echo "  Waiting for distribution to reach 'Deployed' status..."
    if wait_for_dist "${DIST_ID}" "Deployed" 30; then
      echo ""
      echo "  ✓ Distribution fully deployed with Lambda@Edge removed."
    else
      echo "  ⚠ Timed out waiting for 'Deployed' — proceeding anyway."
    fi
  fi
fi

# ── Step 4: Delete stacks except edge-lambda and Foundation ──
echo ""
echo "Step 4/4: Deleting CDK stacks (except edge-lambda and Foundation)..."

EDGE_STACK=$(get_edge_lambda_stack)
echo "  Edge-lambda stack: ${EDGE_STACK:-'(not found)'}"
echo ""

# Reverse dependency order: Frontend -> Evaluation -> Ingestion
# FoundationStack is deferred to Phase 2 because it shares dependencies
# with the edge-lambda stack (IAM roles, log groups) and both must be
# deleted together after Lambda@Edge replicas have drained.
for STACK in "${FRONTEND_STACK}" "${EVALUATION_STACK}" "${INGESTION_STACK}"; do
  delete_stack "${STACK}" "${REGION}" || exit 1
done

# ── Save state for Phase 2 ──
printf 'phase1_complete=%s\nedge_stack=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${EDGE_STACK}" > "${STATE_FILE}"

echo ""
echo "✓ All stacks deleted except the edge-lambda and Foundation stacks."
echo ""
echo "╔══════════════════════════════════════════════════════════════════════╗"
echo "║  WAITING PERIOD REQUIRED                                           ║"
echo "║                                                                    ║"
echo "║  Lambda@Edge replicas are being cleaned up across all edge         ║"
echo "║  locations. This typically takes 30-60 min (sometimes longer).     ║"
echo "║  There is no way to speed this up — it's a CloudFront limitation.  ║"
echo "║                                                                    ║"
echo "║  Deleting too early will fail with:                                ║"
echo "║    'Cannot delete function - replicas still exist'                 ║"
echo "╚══════════════════════════════════════════════════════════════════════╝"
echo ""
# The edge-lambda stack and FoundationStack still exist at this point.
# They share dependencies and will be deleted together in Phase 2.
if [[ -n "${EDGE_STACK}" ]]; then
  echo "  Remaining stacks: ${EDGE_STACK} (us-east-1), ${FOUNDATION_STACK} (${REGION})"
  echo ""
  echo "  To finish cleanup, re-run this script in 30-60 minutes:"
  echo ""
  echo "    ./scripts/delete-app.sh"
  echo ""
else
  echo "  Remaining stack: ${FOUNDATION_STACK} (${REGION})"
  echo "  No edge-lambda stack found."
  echo ""
  echo "  To finish cleanup, re-run this script in 30-60 minutes:"
  echo ""
  echo "    ./scripts/delete-app.sh"
  echo ""
fi
