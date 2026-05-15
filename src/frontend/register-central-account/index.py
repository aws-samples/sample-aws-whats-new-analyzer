"""
Custom resource Lambda handler for auto-registering the Central_Account
in the Account_Registry on deploy.

SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
This code is provided as a reference implementation only.

Writes to the Preferences Table with pk=ACCOUNTS, sk=ACCOUNT#<account_id>.
Idempotent: skips if the record already exists.
"""

import json
import logging
import os
from datetime import datetime, timezone

import boto3

log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

TABLE_NAME = os.environ["PREFERENCES_TABLE"]
CENTRAL_ACCOUNT_ID = os.environ["CENTRAL_ACCOUNT_ID"]
DISPLAY_NAME = os.environ["DISPLAY_NAME"]

ACCOUNTS_PK = "ACCOUNTS"
ACCOUNT_SK_PREFIX = "ACCOUNT#"

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
orgs_client = boto3.client("organizations")


def resolve_account_name():
    """Resolve the real account name from AWS Organizations.

    Falls back to the DISPLAY_NAME env var (deployment prefix) if
    Organizations is not available or the account cannot be found.
    """
    try:
        resp = orgs_client.describe_account(AccountId=CENTRAL_ACCOUNT_ID)
        name = resp.get("Account", {}).get("Name", "")
        if name:
            logger.info("Resolved account name from Organizations: %s", name)
            return name
    except Exception as e:
        logger.info("Could not resolve account name from Organizations: %s", e)
    return DISPLAY_NAME


def handler(event, context):
    """CloudFormation custom resource handler for Central_Account registration."""
    request_type = event.get("RequestType", "")
    logger.info(
        "Custom resource %s for Central_Account %s", request_type, CENTRAL_ACCOUNT_ID
    )

    try:
        if request_type in ("Create", "Update"):
            register_central_account(force_update=(request_type == "Update"))
        # On Delete we intentionally do nothing — the Central_Account
        # record should persist even if the custom resource is removed.
        # The account registry manages its own lifecycle.

        return {
            "PhysicalResourceId": f"central-account-{CENTRAL_ACCOUNT_ID}",
            "Data": {"AccountId": CENTRAL_ACCOUNT_ID},
        }
    except Exception as e:
        logger.warning("Failed to register Central_Account: %s", e, exc_info=True)
        raise


def register_central_account(force_update=False):
    """Register the Central_Account in the Account_Registry (idempotent).

    On Create: inserts the record if it doesn't exist.
    On Update (force_update=True): refreshes the display_name from Organizations.
    """
    sk = f"{ACCOUNT_SK_PREFIX}{CENTRAL_ACCOUNT_ID}"
    display_name = resolve_account_name()

    # Check if the record already exists
    existing = table.get_item(Key={"pk": ACCOUNTS_PK, "sk": sk})
    if "Item" in existing:
        if force_update:
            # Refresh display_name from Organizations on stack updates
            table.update_item(
                Key={"pk": ACCOUNTS_PK, "sk": sk},
                UpdateExpression="SET display_name = :dn, updated_at = :u",
                ExpressionAttributeValues={
                    ":dn": display_name,
                    ":u": datetime.now(timezone.utc).isoformat(),
                },
            )
            logger.info(
                "Updated Central_Account %s display_name to '%s'",
                CENTRAL_ACCOUNT_ID,
                display_name,
            )
        else:
            logger.info(
                "Central_Account %s already registered, skipping",
                CENTRAL_ACCOUNT_ID,
            )
        return

    now = datetime.now(timezone.utc).isoformat()
    item = {
        "pk": ACCOUNTS_PK,
        "sk": sk,
        "account_id": CENTRAL_ACCOUNT_ID,
        "display_name": display_name,
        "enabled": True,
        "is_central": True,
        "created_at": now,
        "updated_at": now,
    }
    table.put_item(Item=item)
    logger.info("Registered Central_Account %s", CENTRAL_ACCOUNT_ID)
