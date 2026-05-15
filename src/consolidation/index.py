"""
Consolidation Lambda.

SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
This code is provided as a reference implementation only.

Reads per-account inventory files and billing dimensions from S3,
computes usage fingerprints, groups accounts by fingerprint, and writes
a consolidated context file to S3.

This Lambda performs no API calls — it only reads from S3 and writes
the merged result back. The Inventory and Billing Lambdas run in parallel
before this step.

Environment variables:
    INVENTORY_BUCKET  — S3 bucket for inventory files
    PREFERENCES_TABLE — DynamoDB table with Account_Registry
    LOG_LEVEL         — Logging level (default: INFO)
"""

import hashlib
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

ACCOUNTS_PK = "ACCOUNTS"
ACCOUNT_SK_PREFIX = "ACCOUNT#"

BILLING_KEY = "inventory/billing-dimensions.json"


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


def read_account_inventory(s3_client, account_id):
    """Read a per-account inventory file from S3.

    Returns the parsed inventory dict, or None if the file is missing.
    """
    key = f"inventory/{account_id}/inventory.json"
    try:
        resp = s3_client.get_object(Bucket=INVENTORY_BUCKET, Key=key)
        body = resp["Body"].read().decode("utf-8")
        data = json.loads(body)
        logger.info(
            "Read inventory for account %s: %d resources",
            account_id,
            data.get("count", 0),
        )
        return data
    except s3_client.exceptions.NoSuchKey:
        logger.warning(
            "Inventory file missing for account %s at s3://%s/%s — skipping",
            account_id,
            INVENTORY_BUCKET,
            key,
        )
        return None


def read_billing_dimensions(s3_client):
    """Read billing dimensions from S3.

    Returns a dict with keys: cache_engines, database_engines, instance_types, platforms, regions.
    Returns empty dimensions if the file is missing (billing Lambda may not have run).
    """
    try:
        resp = s3_client.get_object(Bucket=INVENTORY_BUCKET, Key=BILLING_KEY)
        body = resp["Body"].read().decode("utf-8")
        data = json.loads(body)
        logger.info("Read billing dimensions from s3://%s/%s", INVENTORY_BUCKET, BILLING_KEY)
        return {
            "cache_engines": data.get("cache_engines", []),
            "database_engines": data.get("database_engines", []),
            "instance_types": data.get("instance_types", []),
            "platforms": data.get("platforms", []),
            "regions": data.get("regions", []),
        }
    except s3_client.exceptions.NoSuchKey:
        logger.warning(
            "Billing dimensions file missing at s3://%s/%s — using empty dimensions",
            INVENTORY_BUCKET,
            BILLING_KEY,
        )
        return {
            "cache_engines": [],
            "database_engines": [],
            "instance_types": [],
            "platforms": [],
            "regions": [],
        }


def extract_account_data(inventory):
    """Extract services, resource_types, and grouped inventory from a per-account inventory.

    Returns (services, resource_types, grouped_inventory) where:
    - services: sorted deduplicated list of service names
    - resource_types: sorted deduplicated list of resource type names
    - grouped_inventory: dict of region → service → [resource_types]
    """
    resources = inventory.get("resources", [])
    services = sorted(set(r.get("service", "") for r in resources if r.get("service")))
    resource_types = sorted(
        set(r.get("resource_type", "") for r in resources if r.get("resource_type"))
    )

    # Build grouped structure: region → service → [resource_types]
    grouped = defaultdict(lambda: defaultdict(set))
    for r in resources:
        region = r.get("region", "")
        service = r.get("service", "")
        rt = r.get("resource_type", "")
        if region and service:
            if rt:
                grouped[region][service].add(rt)
            else:
                grouped[region][service]  # ensure service key exists

    # Convert sets to sorted lists for JSON serialization
    grouped_inventory = {}
    for region in sorted(grouped.keys()):
        grouped_inventory[region] = {}
        for service in sorted(grouped[region].keys()):
            grouped_inventory[region][service] = sorted(grouped[region][service])

    return services, resource_types, grouped_inventory


def compute_fingerprint(services, resource_types, db_engines, instance_types, platforms):
    """Compute a SHA-256 usage fingerprint for an account."""
    data = sorted(services + resource_types + db_engines + instance_types + platforms)
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()


def build_consolidated_context(accounts_data, ce_dimensions):
    """Build the consolidated context dict from per-account data and billing dimensions."""
    cache_engines = ce_dimensions.get("cache_engines", [])
    db_engines = ce_dimensions.get("database_engines", [])
    instance_types = ce_dimensions.get("instance_types", [])
    platforms = ce_dimensions.get("platforms", [])
    regions = ce_dimensions.get("regions", [])

    all_services = set()
    for acct in accounts_data:
        all_services.update(acct["services"])
    org_wide_services = sorted(all_services)

    accounts_section = {}
    for acct in accounts_data:
        accounts_section[acct["account_id"]] = {
            "display_name": acct["display_name"],
            "services": acct["services"],
            "inventory": acct["grouped_inventory"],
        }

    fingerprint_groups = defaultdict(list)
    for acct in accounts_data:
        fp = compute_fingerprint(
            acct["services"],
            acct["resource_types"],
            db_engines,
            instance_types,
            platforms,
        )
        fingerprint_groups[fp].append(acct)

    account_groups = []
    for fp, group_accounts in fingerprint_groups.items():
        account_ids = sorted(acct["account_id"] for acct in group_accounts)
        representative = group_accounts[0]
        account_groups.append({
            "fingerprint": fp,
            "account_ids": account_ids,
            "representative_account_id": account_ids[0],
            "services": representative["services"],
            "cache_engines": cache_engines,
            "database_engines": db_engines,
            "instance_types": instance_types,
            "platforms": platforms,
        })

    account_groups.sort(key=lambda g: g["fingerprint"])

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "org_wide": {
            "services": org_wide_services,
            "cache_engines": cache_engines,
            "database_engines": db_engines,
            "instance_types": instance_types,
            "platforms": platforms,
            "regions": regions,
        },
        "account_groups": account_groups,
        "accounts": accounts_section,
    }


def write_consolidated_context(s3_client, context):
    """Write the consolidated context JSON to S3."""
    key = "inventory/consolidated-context.json"
    s3_client.put_object(
        Bucket=INVENTORY_BUCKET,
        Key=key,
        Body=json.dumps(context, indent=2),
        ContentType="application/json",
    )
    logger.info("Wrote consolidated context to s3://%s/%s", INVENTORY_BUCKET, key)


def lambda_handler(event, context):
    """Consolidation Lambda entry point.

    1. Read enabled accounts from the Account_Registry
    2. Read per-account inventory files from S3
    3. Read billing dimensions from S3
    4. Build and write the consolidated context file
    """
    s3_client = boto3.client("s3")
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(PREFERENCES_TABLE)

    accounts = get_enabled_accounts(table)
    logger.info("Found %d enabled accounts in the registry", len(accounts))

    if not accounts:
        logger.warning("No enabled accounts — writing empty consolidated context")
        empty_context = build_consolidated_context([], {
            "database_engines": [],
            "instance_types": [],
            "platforms": [],
            "regions": [],
        })
        write_consolidated_context(s3_client, empty_context)
        return {"accounts_processed": 0, "accounts_skipped": 0, "account_groups": 0}

    # Read per-account inventory files
    accounts_data = []
    skipped = 0

    for account in accounts:
        account_id = account["account_id"]
        display_name = account.get("display_name", "")

        inventory = read_account_inventory(s3_client, account_id)
        if inventory is None:
            skipped += 1
            continue

        services, resource_types, grouped_inventory = extract_account_data(inventory)
        accounts_data.append({
            "account_id": account_id,
            "display_name": display_name,
            "services": services,
            "resource_types": resource_types,
            "grouped_inventory": grouped_inventory,
        })

    logger.info("Read inventory for %d accounts, skipped %d", len(accounts_data), skipped)

    # Read billing dimensions from S3 (written by the Billing Lambda)
    ce_dimensions = read_billing_dimensions(s3_client)

    # Build and write consolidated context
    consolidated = build_consolidated_context(accounts_data, ce_dimensions)
    write_consolidated_context(s3_client, consolidated)

    num_groups = len(consolidated["account_groups"])
    logger.info(
        "Consolidation complete: %d account groups covering %d accounts",
        num_groups,
        len(accounts_data),
    )

    return {
        "accounts_processed": len(accounts_data),
        "accounts_skipped": skipped,
        "account_groups": num_groups,
    }
