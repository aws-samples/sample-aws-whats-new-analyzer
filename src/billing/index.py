"""
Billing Dimensions Lambda.

SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
This code is provided as a reference implementation only.

Queries Cost Explorer for org-wide billing dimensions and writes them to S3.
Runs in parallel with the Inventory Lambda — the Consolidation Lambda reads
both outputs.

For REGION, we use GetCostAndUsage grouped by REGION and apply a minimum spend
threshold ($1/month) to exclude regions that only have default security tooling
(GuardDuty, CloudTrail, Config, etc.) but no real workloads. Customers who have
legitimate low-spend workloads in a region can use the preferences table to
explicitly mark those regions as relevant.

Environment variables:
    INVENTORY_BUCKET — S3 bucket for inventory files
    LOG_LEVEL        — Logging level (default: INFO)
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3

log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

INVENTORY_BUCKET = os.environ["INVENTORY_BUCKET"]

# Dimensions still fetched via GetDimensionValues (no threshold needed)
CE_DIMENSIONS = ["CACHE_ENGINE", "DATABASE_ENGINE", "INSTANCE_TYPE", "PLATFORM"]

DIMENSION_FIELD_MAP = {
    "CACHE_ENGINE": "cache_engines",
    "DATABASE_ENGINE": "database_engines",
    "INSTANCE_TYPE": "instance_types",
    "PLATFORM": "platforms",
}

BILLING_KEY = "inventory/billing-dimensions.json"

# Minimum monthly spend (USD) for a region to be considered "in use".
# Regions below this threshold are typically only running org-wide security
# defaults (GuardDuty, CloudTrail, Config) and are not meaningful for
# announcement relevance. Customers with legitimate low-spend workloads in a
# region should add a preference statement (e.g. "I use ap-south-1 for testing").
REGION_SPEND_THRESHOLD_USD = 1.00


def query_dimension(ce_client, dimension_key):
    """Query Cost Explorer for a single dimension's values over the last 30 days."""
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")

    resp = ce_client.get_dimension_values(
        TimePeriod={"Start": start, "End": end},
        Dimension=dimension_key,
    )
    values = sorted(dv["Value"] for dv in resp["DimensionValues"])
    logger.info("Cost Explorer dimension %s: %d values", dimension_key, len(values))
    return values


def query_regions_by_spend(ce_client):
    """Query Cost Explorer for regions with meaningful spend (> threshold).

    Uses GetCostAndUsage grouped by REGION over the last 30 days. Only regions
    whose total blended cost exceeds REGION_SPEND_THRESHOLD_USD are returned.
    This filters out regions that only have default security tooling (GuardDuty,
    CloudTrail, Config, KMS default keys, etc.) running with negligible cost.
    """
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")

    resp = ce_client.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["BlendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "REGION"}],
    )

    # Aggregate spend across time periods (may span two months)
    region_spend = {}
    for period in resp.get("ResultsByTime", []):
        for group in period.get("Groups", []):
            region = group["Keys"][0]
            amount = float(group["Metrics"]["BlendedCost"]["Amount"])
            region_spend[region] = region_spend.get(region, 0.0) + amount

    # Filter to regions above threshold, exclude empty/global pseudo-regions
    active_regions = sorted(
        region for region, spend in region_spend.items()
        if spend > REGION_SPEND_THRESHOLD_USD
        and region not in ("", "global", "NoRegion")
    )

    logger.info(
        "Regions with spend > $%.2f: %d of %d total (threshold filtered %d)",
        REGION_SPEND_THRESHOLD_USD,
        len(active_regions),
        len(region_spend),
        len(region_spend) - len(active_regions),
    )

    return active_regions


def lambda_handler(event, context):
    """Query all billing dimensions and write to S3."""
    ce_client = boto3.client("ce")
    s3_client = boto3.client("s3")

    dimensions = {}
    for dim in CE_DIMENSIONS:
        field = DIMENSION_FIELD_MAP[dim]
        dimensions[field] = query_dimension(ce_client, dim)

    # Regions use spend-based filtering instead of raw dimension values
    dimensions["regions"] = query_regions_by_spend(ce_client)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **dimensions,
    }

    s3_client.put_object(
        Bucket=INVENTORY_BUCKET,
        Key=BILLING_KEY,
        Body=json.dumps(payload, indent=2),
        ContentType="application/json",
    )

    logger.info("Wrote billing dimensions to s3://%s/%s", INVENTORY_BUCKET, BILLING_KEY)

    return {
        "bucket": INVENTORY_BUCKET,
        "key": BILLING_KEY,
        "dimensions": list(dimensions.keys()),
    }
