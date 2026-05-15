"""
Lambda handler for Account Registry CRUD operations.

SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
This code is provided as a reference implementation only.

Manages multi-account registration in the Preferences Table.
Sits behind API Gateway with Cognito auth.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

TABLE_NAME = os.environ["PREFERENCES_TABLE"]
CENTRAL_ACCOUNT_ID = os.environ.get("CENTRAL_ACCOUNT_ID", "")

ACCOUNTS_PK = "ACCOUNTS"
ACCOUNT_SK_PREFIX = "ACCOUNT#"
ACCOUNT_ID_PATTERN = re.compile(r"^\d{12}$")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
orgs_client = boto3.client("organizations")


def validate_account_id(account_id):
    """Return True if account_id is a string of exactly 12 digits."""
    return isinstance(account_id, str) and bool(ACCOUNT_ID_PATTERN.match(account_id))


def lambda_handler(event, context):
    method = (
        event.get("httpMethod")
        or event.get("requestContext", {}).get("http", {}).get("method")
    )
    body = json.loads(event.get("body") or "{}")
    params = event.get("queryStringParameters") or {}

    logger.info("Received %s request", method)

    try:
        if method == "GET":
            # GET /accounts?lookup=<account_id> — resolve org name for an account
            if params.get("lookup"):
                return lookup_account(params["lookup"])
            # GET /accounts?list_org=true — list all org accounts available to add
            if params.get("list_org"):
                return list_org_accounts()
            return list_accounts()
        elif method == "POST":
            return create_account(body)
        elif method == "PUT":
            return update_account(body)
        elif method == "DELETE":
            return delete_account(body)
        else:
            return response(405, {"error": f"Method {method} not allowed"})
    except Exception as e:
        logger.error("Error handling %s: %s", method, e, exc_info=True)
        return response(500, {"error": str(e)})


def list_accounts():
    """Return all registered accounts."""
    result = table.query(
        KeyConditionExpression=Key("pk").eq(ACCOUNTS_PK)
        & Key("sk").begins_with(ACCOUNT_SK_PREFIX),
    )
    items = [_format_account(item) for item in result.get("Items", [])]
    logger.info("Listed %d accounts", len(items))
    return response(200, items)


def lookup_account(account_id):
    """Look up an account's name from AWS Organizations.

    Returns ``{"account_id": "...", "name": "..."}`` on success, or a 400
    error when the account cannot be resolved.
    """
    if not validate_account_id(account_id):
        return response(400, {"error": "Account ID must be exactly 12 digits"})
    try:
        org_name = validate_org_account(account_id)
        return response(200, {"account_id": account_id, "name": org_name})
    except ValueError as e:
        return response(400, {"error": str(e)})


def list_org_accounts():
    """Return all ACTIVE accounts from AWS Organizations.

    Excludes accounts that are already registered so the picker only shows
    accounts available to add.  Returns an empty list when Organizations
    is not enabled.
    """
    try:
        # Collect all org accounts via paginator
        paginator = orgs_client.get_paginator("list_accounts")
        org_accounts = []
        for page in paginator.paginate():
            for acct in page.get("Accounts", []):
                if acct.get("Status") == "ACTIVE":
                    org_accounts.append(
                        {
                            "account_id": acct["Id"],
                            "name": acct.get("Name", ""),
                        }
                    )
    except orgs_client.exceptions.AWSOrganizationsNotInUseException:
        logger.info("AWS Organizations not in use, returning empty list")
        return response(200, [])

    # Fetch already-registered account IDs to exclude them
    result = table.query(
        KeyConditionExpression=Key("pk").eq(ACCOUNTS_PK)
        & Key("sk").begins_with(ACCOUNT_SK_PREFIX),
        ProjectionExpression="account_id",
    )
    registered_ids = {item["account_id"] for item in result.get("Items", [])}

    available = [a for a in org_accounts if a["account_id"] not in registered_ids]
    available.sort(key=lambda a: a["name"].lower())
    logger.info(
        "Listed %d org accounts (%d already registered)",
        len(available),
        len(registered_ids),
    )
    return response(200, available)


def validate_org_account(account_id):
    """Verify the account exists in the AWS Organization.

    Returns the account name from Organizations if valid, or raises
    ValueError with a user-friendly message if not.
    """
    try:
        resp = orgs_client.describe_account(AccountId=account_id)
        acct = resp.get("Account", {})
        status = acct.get("Status", "")
        if status != "ACTIVE":
            raise ValueError(
                f"Account {account_id} exists but is {status}, not ACTIVE"
            )
        return acct.get("Name", "")
    except orgs_client.exceptions.AccountNotFoundException:
        raise ValueError(
            f"Account {account_id} is not a member of this Organization"
        )
    except orgs_client.exceptions.AWSOrganizationsNotInUseException:
        # No Organization — the central (deployment) account is already
        # auto-registered by the custom resource, so there are no other
        # valid accounts to add.
        raise ValueError(
            "AWS Organizations is not enabled. "
            "Only the deployment account can be used and it is "
            "registered automatically."
        )


def create_account(body):
    """Register a new account.

    Validates that the account is a member of the AWS Organization before
    registering. If the deployment account is not part of an Organization,
    validation is skipped.
    """
    account_id = body.get("account_id", "")
    display_name = body.get("display_name", "")

    if not validate_account_id(account_id):
        return response(400, {"error": "Account ID must be exactly 12 digits"})

    # Validate against AWS Organizations
    try:
        org_name = validate_org_account(account_id)
    except ValueError as e:
        return response(400, {"error": str(e)})

    # Use the org account name as fallback if no display name provided
    if not display_name and org_name:
        display_name = org_name

    is_central = account_id == CENTRAL_ACCOUNT_ID

    now = datetime.now(timezone.utc).isoformat()
    item = {
        "pk": ACCOUNTS_PK,
        "sk": f"{ACCOUNT_SK_PREFIX}{account_id}",
        "account_id": account_id,
        "display_name": display_name,
        "enabled": True,
        "is_central": is_central,
        "created_at": now,
        "updated_at": now,
    }
    table.put_item(Item=item)
    logger.info("Created account %s (is_central=%s)", account_id, is_central)
    return response(201, _format_account(item))


def update_account(body):
    """Update an existing account's display name, enabled status, or aggregator region."""
    account_id = body.get("account_id", "")
    if not validate_account_id(account_id):
        return response(400, {"error": "Account ID must be exactly 12 digits"})

    sk = f"{ACCOUNT_SK_PREFIX}{account_id}"
    now = datetime.now(timezone.utc).isoformat()

    update_parts = ["updated_at = :u"]
    attr_values = {":u": now}

    if "display_name" in body:
        update_parts.append("display_name = :dn")
        attr_values[":dn"] = body["display_name"]

    if "enabled" in body:
        update_parts.append("enabled = :en")
        attr_values[":en"] = body["enabled"]

    result = table.update_item(
        Key={"pk": ACCOUNTS_PK, "sk": sk},
        UpdateExpression="SET " + ", ".join(update_parts),
        ExpressionAttributeValues=attr_values,
        ConditionExpression="attribute_exists(pk)",
        ReturnValues="ALL_NEW",
    )
    logger.info("Updated account %s", account_id)
    return response(200, _format_account(result["Attributes"]))


def delete_account(body):
    """Delete an account. Central account cannot be deleted."""
    account_id = body.get("account_id", "")
    if not validate_account_id(account_id):
        return response(400, {"error": "Account ID must be exactly 12 digits"})

    sk = f"{ACCOUNT_SK_PREFIX}{account_id}"

    # Check if this is the central account
    existing = table.get_item(Key={"pk": ACCOUNTS_PK, "sk": sk})
    item = existing.get("Item")
    if item and item.get("is_central"):
        logger.warning("Attempted to delete central account %s", account_id)
        return response(403, {"error": "Central account cannot be deleted"})

    table.delete_item(Key={"pk": ACCOUNTS_PK, "sk": sk})
    logger.info("Deleted account %s", account_id)
    return response(200, {"deleted": account_id})


def _format_account(item):
    """Format a DynamoDB item into the API response shape."""
    return {
        "account_id": item.get("account_id", ""),
        "display_name": item.get("display_name", ""),
        "enabled": item.get("enabled", False),
        "is_central": item.get("is_central", False),
        "created_at": item.get("created_at", ""),
        "updated_at": item.get("updated_at", ""),
    }


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
        },
        "body": json.dumps(body, default=str),
    }
