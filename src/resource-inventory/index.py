"""
Per-Account Inventory Collection Lambda.

SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
This code is provided as a reference implementation only.

Uses a Resource Explorer organization-scoped view to collect resource inventory
across all accounts in a single Search call. Each resource's OwningAccountId
is used to bucket results per account. Only accounts registered and enabled in
the Account_Registry get their inventory written to S3.

No cross-account IAM roles or STS AssumeRole calls are needed — the org-scoped
view returns resources from all member accounts directly.

Returns a summary: { "successful": N, "failed": M, "accounts": [...] }
"""

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr, Key

log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

INVENTORY_BUCKET = os.environ["INVENTORY_BUCKET"]
PREFERENCES_TABLE = os.environ["PREFERENCES_TABLE"]
CENTRAL_ACCOUNT_ID = os.environ["CENTRAL_ACCOUNT_ID"]
RESOURCE_EXPLORER_VIEW_ARN = os.environ["RESOURCE_EXPLORER_VIEW_ARN"]

ACCOUNTS_PK = "ACCOUNTS"
ACCOUNT_SK_PREFIX = "ACCOUNT#"


def derive_region_from_view_arn(view_arn):
    """Extract the region from a Resource Explorer view ARN.

    ARN format: arn:aws:resource-explorer-2:<region>:<account>:view/<name>/<id>
    """
    parts = view_arn.split(":")
    if len(parts) < 4 or not parts[3]:
        raise ValueError(f"Cannot derive region from view ARN: {view_arn}")
    return parts[3]


def get_enabled_accounts(table):
    """Query the Account_Registry for all accounts with enabled=true."""
    result = table.query(
        KeyConditionExpression=(
            Key("pk").eq(ACCOUNTS_PK)
            & Key("sk").begins_with(ACCOUNT_SK_PREFIX)
        ),
        FilterExpression=Attr("enabled").eq(True),
    )
    return result.get("Items", [])


def build_inventory_key(account_id):
    """Build the S3 key for a per-account inventory file."""
    return f"inventory/{account_id}/inventory.json"


def search_all_resources(view_arn):
    """Search Resource Explorer using the org-scoped view, paginating through all results.

    The API client region is derived from the view ARN.

    Returns a dict mapping account_id -> set of (region, service, resource_type) tuples.
    """
    region = derive_region_from_view_arn(view_arn)
    client = boto3.client("resource-explorer-2", region_name=region)

    per_account = defaultdict(set)
    next_token = None

    while True:
        params = {"QueryString": "*", "MaxResults": 1000, "ViewArn": view_arn}
        if next_token:
            params["NextToken"] = next_token

        response = client.search(**params)

        for resource in response["Resources"]:
            account_id = resource["OwningAccountId"]
            key = (
                resource["Region"],
                resource["Service"],
                resource["ResourceType"],
            )
            per_account[account_id].add(key)

        next_token = response.get("NextToken")
        if not next_token:
            break

    return per_account


def write_inventory_to_s3(s3_client, account_id, resources):
    """Write per-account inventory JSON to S3."""
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "account_id": account_id,
        "count": len(resources),
        "resources": resources,
    }
    key = build_inventory_key(account_id)
    s3_client.put_object(
        Bucket=INVENTORY_BUCKET,
        Key=key,
        Body=json.dumps(payload, indent=2),
        ContentType="application/json",
    )
    logger.info(
        "Wrote %d resources for account %s to s3://%s/%s",
        len(resources),
        account_id,
        INVENTORY_BUCKET,
        key,
    )
    return key


def lambda_handler(event, context):
    """Collect resource inventory for all enabled accounts in the registry.

    Uses a single Resource Explorer org-scoped view Search call to get resources
    across all accounts, then writes per-account inventory files for each
    enabled account in the registry.
    """
    s3_client = boto3.client("s3")
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(PREFERENCES_TABLE)

    accounts = get_enabled_accounts(table)
    logger.info("Found %d enabled accounts in the registry", len(accounts))

    if not accounts:
        raise RuntimeError("No enabled accounts found in the registry — aborting")

    per_account_resources = search_all_resources(RESOURCE_EXPLORER_VIEW_ARN)
    logger.info(
        "Org-wide search returned resources for %d accounts",
        len(per_account_resources),
    )

    results = []
    for account in accounts:
        account_id = account["account_id"]
        raw = per_account_resources.get(account_id, set())
        sorted_tuples = sorted(raw)
        resources = [
            {"region": r, "service": s, "resource_type": rt}
            for r, s, rt in sorted_tuples
        ]

        key = write_inventory_to_s3(s3_client, account_id, resources)
        results.append({
            "account_id": account_id,
            "status": "success",
            "key": key,
            "count": len(resources),
        })

    logger.info(
        "Inventory collection complete: %d accounts written",
        len(results),
    )

    return {
        "successful": len(results),
        "failed": 0,
        "accounts": results,
    }
