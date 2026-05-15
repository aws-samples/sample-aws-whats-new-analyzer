"""
Evaluation Pipeline Lambda.

SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
This code is provided as a reference implementation only.

Provides multiple handler functions invoked by the evaluation Step Functions
state machine at different stages of the pipeline:

  - sqs_trigger_handler:       Receives SQS messages, starts state machine execution
  - pre_filter_handler:        Invokes Awana runtime with mode=pre-filter
  - account_agnostic_handler:  Invokes Awana runtime with mode=evaluate (no account_group)
  - store_all_accounts_handler: Stores results for all enabled accounts
  - read_groups_handler:       Reads account_groups from consolidated-context.json
  - per_group_handler:         Invokes Awana runtime with mode=evaluate per account group

Environment variables:
    AGENT_RUNTIME_ARN — Awana AgentCore runtime ARN (passed directly as env var)
    RESULTS_TABLE       — DynamoDB results table name
    INVENTORY_BUCKET    — S3 bucket for inventory / consolidated context
    PREFERENCES_TABLE   — DynamoDB preferences table name
    TOPIC_ARN           — SNS topic for relevant announcements
    ALERTS_TOPIC_ARN    — SNS topic for operational alerts
    CENTRAL_ACCOUNT_ID  — Central account ID
    STATE_MACHINE_ARN   — Evaluation state machine ARN (for SQS trigger)
    LOG_LEVEL           — Logging level (default: INFO)
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError

log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ─── AWS clients (initialised once per container) ───

agent_core_client = boto3.client("bedrock-agentcore")
dynamodb_resource = boto3.resource("dynamodb")
s3_client = boto3.client("s3")
sns_client = boto3.client("sns")
sfn_client = boto3.client("stepfunctions")

# ─── Environment ───

AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
RESULTS_TABLE = os.environ.get("RESULTS_TABLE", "")
INVENTORY_BUCKET = os.environ.get("INVENTORY_BUCKET", "")
PREFERENCES_TABLE = os.environ.get("PREFERENCES_TABLE", "")
TOPIC_ARN = os.environ.get("TOPIC_ARN", "")
ALERTS_TOPIC_ARN = os.environ.get("ALERTS_TOPIC_ARN", "")
CENTRAL_ACCOUNT_ID = os.environ.get("CENTRAL_ACCOUNT_ID", "")
STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")

ACCOUNTS_PK = "ACCOUNTS"
ACCOUNT_SK_PREFIX = "ACCOUNT#"
CONTEXT_FILE_KEY = "inventory/consolidated-context.json"


# ─── Circuit breaker ───

CIRCUIT_BREAKER_ERROR_CODES = {
    "ResourceNotFoundException",   # Agent runtime deleted or ARN wrong
    "AccessDeniedException",       # IAM permissions revoked
    "ServiceUnavailableException", # AgentCore down in region
    "ValidationException",         # Runtime in CREATE_FAILED or similar non-invocable state
}

SQS_TRIGGER_FUNCTION_NAME = os.environ.get("SQS_TRIGGER_FUNCTION_NAME", "")
lambda_client = boto3.client("lambda")


def trip_circuit_breaker(error_msg: str):
    """Disable the SQS trigger Lambda's event source mapping and send an alert.

    This stops new announcements from entering the evaluation pipeline until
    the underlying issue (agent runtime failure, permissions, etc.) is resolved.
    """
    logger.error("CIRCUIT BREAKER TRIPPED: %s", error_msg)

    if SQS_TRIGGER_FUNCTION_NAME:
        try:
            mappings = lambda_client.list_event_source_mappings(
                FunctionName=SQS_TRIGGER_FUNCTION_NAME
            )
            for mapping in mappings.get("EventSourceMappings", []):
                if "sqs" in mapping.get("EventSourceArn", "").lower():
                    lambda_client.update_event_source_mapping(
                        UUID=mapping["UUID"],
                        Enabled=False,
                    )
                    logger.info("Disabled SQS event source mapping %s on %s", mapping["UUID"], SQS_TRIGGER_FUNCTION_NAME)
        except Exception as e:
            logger.error("Failed to disable SQS trigger event source: %s", e, exc_info=True)

    if ALERTS_TOPIC_ARN:
        try:
            sns_client.publish(
                TopicArn=ALERTS_TOPIC_ARN,
                Subject="Awana Circuit Breaker Tripped",
                Message=(
                    f"The Awana evaluation pipeline circuit breaker has been tripped.\n\n"
                    f"Reason: {error_msg}\n\n"
                    f"The SQS trigger event source has been disabled. Messages will\n"
                    f"accumulate in the queue until the issue is resolved.\n\n"
                    f"To re-enable after fixing:\n"
                    f"  aws lambda list-event-source-mappings "
                    f"--function-name {SQS_TRIGGER_FUNCTION_NAME}\n"
                    f"  aws lambda update-event-source-mapping --uuid <UUID> --enabled\n\n"
                    f"Timestamp: {datetime.now(timezone.utc).isoformat()}"
                ),
            )
        except Exception as e:
            logger.error("Failed to send circuit breaker alert: %s", e, exc_info=True)


# ─── Helper: invoke Awana runtime ───


def _invoke_awana(payload_dict: dict) -> dict:
    """Invoke the Awana AgentCore runtime and return the parsed response.

    Parameters
    ----------
    payload_dict : dict
        The ``input`` dict sent inside ``{"input": ...}``.

    Returns
    -------
    dict
        Parsed JSON response from the runtime.

    Raises
    ------
    ValueError
        If AGENT_RUNTIME_ARN is not configured.
    """
    if not AGENT_RUNTIME_ARN:
        raise ValueError("AGENT_RUNTIME_ARN environment variable is not set")

    payload = json.dumps({"input": payload_dict}).encode()
    session_id = str(uuid.uuid4())

    logger.info(
        "Invoking Awana runtime (mode=%s, session=%s)",
        payload_dict.get("mode", "evaluate"),
        session_id,
    )

    try:
        response = agent_core_client.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            runtimeSessionId=session_id,
            payload=payload,
        )
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code in CIRCUIT_BREAKER_ERROR_CODES:
            trip_circuit_breaker(f"Agent {error_code}: {e}")
        elif e.response.get('ResponseMetadata', {}).get('HTTPStatusCode', 0) >= 500:
            http_status = e.response.get('ResponseMetadata', {}).get('HTTPStatusCode', 0)
            trip_circuit_breaker(f"Agent returned HTTP {http_status}: {e}")
        raise
    except BotoCoreError as e:
        trip_circuit_breaker(f"Agent infrastructure failure ({type(e).__name__}): {e}")
        raise

    chunks: list[str] = []
    if response.get("contentType") == "application/json":
        for chunk in response.get("response", []):
            chunks.append(chunk.decode("utf-8"))

    result_text = "".join(chunks)
    logger.info("Awana response (%d chars): %s", len(result_text), result_text[:300])

    try:
        parsed = json.loads(result_text)
        if "output" in parsed:
            return parsed["output"]
        return parsed
    except (json.JSONDecodeError, TypeError):
        logger.warning("Could not parse Awana response as JSON, returning raw text")
        return {"result": result_text, "reasoning": ""}


# ─── Core logic functions ───


def handle_pre_filter(announcement: dict) -> dict:
    """Invoke Awana runtime with ``mode: "pre-filter"``.

    Returns ``{"pass": bool, "reason": str}``.
    On failure, defaults to pass-through (safe default).
    """
    try:
        result = _invoke_awana({
            "prompt": json.dumps(announcement),
            "mode": "pre-filter",
        })
        return {
            "pass": result.get("pass", True),
            "reason": result.get("reason", ""),
        }
    except Exception as e:
        logger.error("Pre-filter invocation failed: %s — passing through", e, exc_info=True)
        return {"pass": True, "reason": f"Pre-filter error, passing through: {e}"}


def handle_account_agnostic_eval(announcement: dict) -> dict:
    """Invoke Awana runtime with ``mode: "evaluate"`` (no account_group).

    Returns ``{"result": str, "reasoning": str}``.
    On failure, raises to let Step Functions retry.
    """
    result = _invoke_awana({
        "prompt": json.dumps(announcement),
        "mode": "evaluate",
    })
    return {
        "result": result.get("result", ""),
        "reasoning": result.get("reasoning", ""),
    }


def handle_per_group_eval(announcement: dict, account_group: dict) -> dict:
    """Invoke Awana runtime with ``mode: "evaluate"`` and account_group context.

    Returns ``{"result": str, "reasoning": str}``.
    On failure, logs and returns an error result (does not raise).
    """
    fingerprint = account_group.get("fingerprint", "unknown")
    try:
        result = _invoke_awana({
            "prompt": json.dumps(announcement),
            "mode": "evaluate",
            "account_group": account_group,
        })
        return {
            "result": result.get("result", ""),
            "reasoning": result.get("reasoning", ""),
        }
    except Exception as e:
        account_ids = account_group.get("account_ids", [])
        logger.error(
            "Per-group eval failed for fingerprint=%s, accounts=%s: %s",
            fingerprint,
            account_ids,
            e,
            exc_info=True,
        )
        return {
            "result": "error",
            "reasoning": f"Evaluation failed: {e}",
        }


def store_results_for_all_accounts(announcement: dict, result: str, reasoning: str, account_ids: list[str], group_fingerprint: str = ""):
    """Store one result item per account in the Results_Table.

    Each item shares the same result/reasoning but has a unique
    ``account_id`` and sort key ``<account_id>#<timestamp>``.
    On failure for an individual account, logs and continues.
    """
    table = dynamodb_resource.Table(RESULTS_TABLE)
    announcement_id = announcement.get("link", announcement.get("id", "unknown"))
    is_relevant = result.lower().strip() == "relevant"
    now = datetime.now(timezone.utc).isoformat()
    # Pull announcement publish date for the chronological GSI (ByPubDate).
    # Items missing pubDate simply won't appear in the GSI — that's intentional
    # since they have no date to sort by.
    pub_date = announcement.get("pubDate", "") if isinstance(announcement, dict) else ""
    stored = 0

    for account_id in account_ids:
        try:
            item = {
                "id": announcement_id,
                "timestamp": f"{account_id}#{now}",
                "account_id": account_id,
                "announcement": announcement,
                "agent_response": json.dumps({"result": result, "reasoning": reasoning}),
                "result": result,
                "reasoning": reasoning,
                "is_relevant": is_relevant,
                "group_fingerprint": group_fingerprint,
                "ttl": int(datetime.now(timezone.utc).timestamp()) + 365 * 86400,
            }
            # Only project into the GSI when pubDate is present.
            # DynamoDB requires both GSI key attributes to be set for the item
            # to appear in the index; omitting them leaves the item out cleanly.
            if pub_date:
                item["gsi_pk"] = "ALL"
                item["pubDate"] = pub_date
            table.put_item(Item=item)
            stored += 1
        except Exception as e:
            logger.error(
                "Failed to store result for account %s, announcement %s: %s",
                account_id,
                announcement_id,
                e,
                exc_info=True,
            )

    logger.info(
        "Stored results for %d/%d accounts (announcement=%s, result=%s)",
        stored,
        len(account_ids),
        announcement_id,
        result,
    )

    # Publish to SNS if relevant
    if is_relevant:
        try:
            sns_client.publish(
                TopicArn=TOPIC_ARN,
                Message=json.dumps(announcement),
                Subject=f"Relevant: {announcement.get('title', 'AWS Announcement')}"[:100],
            )
        except Exception as e:
            logger.error("Failed to publish relevant announcement to SNS: %s", e, exc_info=True)

    return {"stored": stored, "total": len(account_ids)}


def store_results_for_group(announcement: dict, result: str, reasoning: str, account_group: dict):
    """Store results for all accounts in an Account_Group.

    Delegates to ``store_results_for_all_accounts`` with the group's account IDs.
    """
    account_ids = account_group.get("account_ids", [])
    fingerprint = account_group.get("fingerprint", "")
    return store_results_for_all_accounts(
        announcement, result, reasoning, account_ids, group_fingerprint=fingerprint,
    )


def read_account_groups() -> list[dict]:
    """Read ``inventory/consolidated-context.json`` from S3 and extract account_groups."""
    try:
        resp = s3_client.get_object(Bucket=INVENTORY_BUCKET, Key=CONTEXT_FILE_KEY)
        body = resp["Body"].read().decode("utf-8")
        context = json.loads(body)
        groups = context.get("account_groups", [])
        logger.info("Read %d account groups from consolidated context", len(groups))
        return groups
    except Exception as e:
        logger.error("Failed to read consolidated context from S3: %s", e, exc_info=True)
        return []


def get_all_enabled_account_ids() -> list[str]:
    """Query Account_Registry for all enabled account IDs."""
    table = dynamodb_resource.Table(PREFERENCES_TABLE)
    try:
        result = table.query(
            KeyConditionExpression=(
                Key("pk").eq(ACCOUNTS_PK)
                & Key("sk").begins_with(ACCOUNT_SK_PREFIX)
            ),
            FilterExpression=Attr("enabled").eq(True),
        )
        account_ids = [item.get("account_id", "") for item in result.get("Items", []) if item.get("account_id")]
        logger.info("Found %d enabled accounts in registry", len(account_ids))
        return account_ids
    except Exception as e:
        logger.error("Failed to query Account_Registry: %s", e, exc_info=True)
        return []


# ─── Step Functions handler functions ───


def sqs_trigger_handler(event, context):
    """Thin handler: receives SQS messages and starts the evaluation state machine.

    Each SQS record triggers a separate state machine execution so that
    announcements are evaluated independently.
    """
    logger.info("SQS trigger received %d records", len(event.get("Records", [])))

    for record in event.get("Records", []):
        message = json.loads(record["body"])
        announcement_id = message.get("link", message.get("id", "unknown"))

        try:
            execution_name = f"eval-{uuid.uuid4().hex[:20]}"
            sfn_client.start_execution(
                stateMachineArn=STATE_MACHINE_ARN,
                name=execution_name,
                input=json.dumps({"announcement": message}),
            )
            logger.info(
                "Started evaluation execution %s for announcement %s",
                execution_name,
                announcement_id,
            )
        except Exception as e:
            logger.warning(
                "Failed to start evaluation execution for %s: %s",
                announcement_id,
                e,
                exc_info=True,
            )
            raise  # Let SQS retry

    return {"statusCode": 200}


def pre_filter_handler(event, context):
    """Step Functions step: run pre-filter on the announcement.

    Input:  ``{"announcement": {...}}``
    Output: ``{"pass": bool, "reason": str, "announcement": {...}}``
    """
    announcement = event.get("announcement", {})
    announcement_id = announcement.get("link", announcement.get("id", "unknown"))
    logger.info("Pre-filter step for announcement %s", announcement_id)

    result = handle_pre_filter(announcement)

    return {
        "pass": result["pass"],
        "reason": result["reason"],
        "announcement": announcement,
    }


def account_agnostic_handler(event, context):
    """Step Functions step: run account-agnostic evaluation.

    Input:  ``{"announcement": {...}, "pass": true, "reason": "..."}``
    Output: ``{"result": str, "reasoning": str, "announcement": {...}}``
    """
    announcement = event.get("announcement", {})
    announcement_id = announcement.get("link", announcement.get("id", "unknown"))
    logger.info("Account-agnostic eval step for announcement %s", announcement_id)

    result = handle_account_agnostic_eval(announcement)

    return {
        "result": result["result"],
        "reasoning": result["reasoning"],
        "announcement": announcement,
    }


def store_all_accounts_handler(event, context):
    """Step Functions step: store results for all enabled accounts.

    Input:  ``{"announcement": {...}, "result": str, "reasoning": str}``
    Output: ``{"stored": int, "total": int}``
    """
    announcement = event.get("announcement", {})
    result = event.get("result", "")
    reasoning = event.get("reasoning", "")
    announcement_id = announcement.get("link", announcement.get("id", "unknown"))

    logger.info(
        "Storing result '%s' for all accounts (announcement=%s)",
        result,
        announcement_id,
    )

    account_ids = get_all_enabled_account_ids()
    if not account_ids:
        logger.warning("No enabled accounts found — nothing to store")
        return {"stored": 0, "total": 0}

    return store_results_for_all_accounts(announcement, result, reasoning, account_ids)


def read_groups_handler(event, context):
    """Step Functions step: read account groups from consolidated context.

    Input:  ``{"announcement": {...}, "result": str, "reasoning": str}``
    Output: ``{"announcement": {...}, "account_groups": [...]}``
    """
    announcement = event.get("announcement", {})
    announcement_id = announcement.get("link", announcement.get("id", "unknown"))

    groups = read_account_groups()

    # Log observability metric: groups vs total accounts
    total_accounts = sum(len(g.get("account_ids", [])) for g in groups)
    logger.info(
        "Evaluating %d groups covering %d accounts (announcement=%s)",
        len(groups),
        total_accounts,
        announcement_id,
    )

    return {
        "announcement": announcement,
        "account_groups": groups,
    }


def classify_handler(event, context):
    """Step Functions step: combined account-agnostic classification.

    Invokes Awana runtime with mode=classify, which internally runs:
    general-category → pre-filter → service-routing in sequence.

    Input:  ``{"announcement": {...}}``
    Output: ``{"decision": str, "result": str, "reasoning": str, "matched_service": str, "services": [...], "announcement": {...}}``
    """
    announcement = event.get("announcement", {})
    announcement_id = announcement.get("link", announcement.get("id", "unknown"))
    logger.info("Classify step for announcement %s", announcement_id)

    try:
        result = _invoke_awana({
            "prompt": json.dumps(announcement),
            "mode": "classify",
        })

        return {
            "decision": result.get("decision", "per_group"),
            "result": result.get("result", ""),
            "reasoning": result.get("reasoning", ""),
            "matched_service": result.get("matched_service", ""),
            "services": result.get("services", []),
            "announcement": announcement,
        }
    except Exception as e:
        logger.error("Classify step failed for %s: %s — defaulting to per_group", announcement_id, e, exc_info=True)
        return {
            "decision": "per_group",
            "result": "",
            "reasoning": f"Classify failed: {e}",
            "matched_service": "",
            "services": [],
            "announcement": announcement,
        }


def service_routing_handler(event, context):
    """Step Functions step: invoke Awana runtime with mode=service-routing.

    Input:  ``{"announcement": {...}}``
    Output: ``{"route": str, "matched_service": str, "services": [...], "announcement": {...}}``
    """
    announcement = event.get("announcement", {})
    announcement_id = announcement.get("link", announcement.get("id", "unknown"))

    try:
        result = _invoke_awana({"prompt": json.dumps(announcement), "mode": "service-routing"})

        route = result.get("route", "multi_service")
        matched_service = result.get("matched_service", "")
        services = result.get("services", [])

        logger.info(
            "Service routing for announcement %s: route=%s, matched_service=%s, services=%s",
            announcement_id,
            route,
            matched_service,
            services,
        )

        return {
            "route": route,
            "matched_service": matched_service,
            "services": services,
            "announcement": announcement,
        }
    except Exception as e:
        logger.error(
            "Service routing failed for announcement %s: %s — falling back to multi_service",
            announcement_id,
            e,
            exc_info=True,
        )
        return {
            "route": "multi_service",
            "matched_service": "",
            "services": [],
            "announcement": announcement,
        }


def store_single_service_handler(event, context):
    """Step Functions step: deterministic per-account matching for single-service announcements.

    Input:  ``{"announcement": {...}, "matched_service": str, "route": "single_service", "services": [...]}``
    Output: ``{"stored": int, "total": int, "matched_accounts": int, "unmatched_accounts": int}``

    Loads the consolidated context from S3 and checks each account's services
    list for the matched service (case-insensitive). Matching accounts get a
    "relevant" result; non-matching accounts get "not relevant".

    If the context file is unavailable, raises an exception to let Step Functions retry.
    """
    announcement = event.get("announcement", {})
    matched_service = event.get("matched_service", "")
    services = event.get("services", [])
    announcement_id = announcement.get("link", announcement.get("id", "unknown"))

    # Load consolidated context — raise on failure so Step Functions can retry
    resp = s3_client.get_object(Bucket=INVENTORY_BUCKET, Key=CONTEXT_FILE_KEY)
    body = resp["Body"].read().decode("utf-8")
    context_data = json.loads(body)

    accounts = context_data.get("accounts", {})
    matched_service_lower = matched_service.lower()

    matching_account_ids = []
    non_matching_account_ids = []

    for account_id, account_info in accounts.items():
        account_services = [s.lower() for s in account_info.get("services", [])]
        if matched_service_lower in account_services:
            matching_account_ids.append(account_id)
        else:
            non_matching_account_ids.append(account_id)

    logger.info(
        "Single-service matching for announcement %s: service=%s, matching=%d, non_matching=%d",
        announcement_id,
        matched_service,
        len(matching_account_ids),
        len(non_matching_account_ids),
    )

    total_stored = 0

    if matching_account_ids:
        result = store_results_for_all_accounts(
            announcement,
            "relevant",
            f"Service {matched_service} is used in this account",
            matching_account_ids,
        )
        total_stored += result.get("stored", 0)

    if non_matching_account_ids:
        result = store_results_for_all_accounts(
            announcement,
            "not relevant",
            f"Service {matched_service} is not used in this account",
            non_matching_account_ids,
        )
        total_stored += result.get("stored", 0)

    total_accounts = len(matching_account_ids) + len(non_matching_account_ids)

    return {
        "stored": total_stored,
        "total": total_accounts,
        "matched_accounts": len(matching_account_ids),
        "unmatched_accounts": len(non_matching_account_ids),
    }


def per_group_handler(event, context):
    """Step Functions Map iteration: evaluate one account group and store results.

    Input (from Map state iterator):
    ``{"announcement": {...}, "group": {"fingerprint": ..., "account_ids": [...], ...}}``

    Output: ``{"fingerprint": str, "result": str, "stored": int, "total": int}``
    """
    announcement = event.get("announcement", {})
    group = event.get("group", {})
    fingerprint = group.get("fingerprint", "unknown")
    account_ids = group.get("account_ids", [])
    announcement_id = announcement.get("link", announcement.get("id", "unknown"))

    logger.info(
        "Per-group eval for fingerprint=%s (%d accounts), announcement=%s",
        fingerprint,
        len(account_ids),
        announcement_id,
    )

    # Evaluate
    eval_result = handle_per_group_eval(announcement, group)
    result = eval_result.get("result", "")
    reasoning = eval_result.get("reasoning", "")

    # Store results for all accounts in the group
    storage = store_results_for_group(announcement, result, reasoning, group)

    return {
        "fingerprint": fingerprint,
        "result": result,
        "stored": storage.get("stored", 0),
        "total": storage.get("total", 0),
    }



# ─── Dispatcher handler (default entry point for Step Functions) ───


def lambda_handler(event, context):
    """Main dispatcher handler invoked by Step Functions.

    Routes to the appropriate handler based on the ``_handler`` field
    in the event payload. Falls back to ``pre_filter_handler`` if no
    ``_handler`` is specified.

    Supported ``_handler`` values:
      - ``pre_filter``
      - ``account_agnostic``
      - ``store_all_accounts``
      - ``read_groups``
      - ``per_group``
      - ``sqs_trigger``
      - ``service_routing``
      - ``store_single_service``
    """
    handler_name = event.get("_handler", "pre_filter")
    logger.info("Dispatcher routing to handler: %s", handler_name)

    handlers = {
        "classify": classify_handler,
        "pre_filter": pre_filter_handler,
        "account_agnostic": account_agnostic_handler,
        "store_all_accounts": store_all_accounts_handler,
        "read_groups": read_groups_handler,
        "per_group": per_group_handler,
        "sqs_trigger": sqs_trigger_handler,
        "service_routing": service_routing_handler,
        "store_single_service": store_single_service_handler,
    }

    handler_fn = handlers.get(handler_name)
    if handler_fn is None:
        logger.error("Unknown handler: %s", handler_name)
        raise ValueError(f"Unknown handler: {handler_name}")

    return handler_fn(event, context)
