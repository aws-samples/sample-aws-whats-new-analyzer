"""
Lambda handler for the Feedback Analyst Agent **and** feedback HTTP CRUD.

SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
This code is provided as a reference implementation only.

Two separate Lambda functions share this code (see ``lib/frontend-stack.ts``):

1. **FeedbackFunction** — API Gateway POST/GET/DELETE for feedback CRUD.
   Stores ``FEEDBACK#`` records in the Preferences Table scoped to the
   ``account_id`` from the evaluated result.

2. **FeedbackAnalystFunction** — DynamoDB Streams trigger that processes
   new/updated ``FEEDBACK#`` records through AgentCore Memory's
   ``userPreferenceMemoryStrategy`` to extract multi-dimensional preference
   signals.

Feedback records are stored with account-scoped partition keys:
  - ``ACCOUNT#<account_id>`` when the result has an account_id
  - ``ACCOUNT#GLOBAL`` when no account_id is present (legacy / standalone)
"""

import json
import logging
import os
import uuid

import boto3
from boto3.dynamodb.conditions import Key

log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

region = os.environ.get('AWS_REGION', 'eu-west-1')
PREFERENCES_TABLE = os.environ.get('PREFERENCES_TABLE', '')
RESULTS_TABLE = os.environ.get('RESULTS_TABLE', '')
INVENTORY_BUCKET = os.environ.get('INVENTORY_BUCKET', '')
MEMORY_ID = os.environ.get('MEMORY_ID', '')
PROMPTS_BUCKET = os.environ.get('PROMPTS_BUCKET', '')
PROMPTS_KEY = os.environ.get('PROMPTS_KEY', 'config/prompts.json')

GLOBAL_PK = 'ACCOUNT#GLOBAL'
ACCOUNT_PK_PREFIX = 'ACCOUNT#'

dynamodb = boto3.resource('dynamodb', region_name=region)
s3_client = boto3.client('s3', region_name=region)

# Cache for prompt config (loaded once per Lambda cold start)
_analyst_config = None


# ---------------------------------------------------------------------------
# Entry point — route between API Gateway events and DynamoDB Stream events
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    """Dispatch to HTTP CRUD or DynamoDB Stream processing."""
    if 'httpMethod' in event or 'requestContext' in event:
        return _handle_http(event)
    if 'Records' in event:
        return _handle_stream(event)
    logger.warning('Unrecognised event shape, returning 200')
    return {'statusCode': 200}


# ===================================================================
# HTTP CRUD — feedback storage (used by FeedbackFunction Lambda)
# ===================================================================

def _handle_http(event):
    method = (
        event.get('httpMethod')
        or event.get('requestContext', {}).get('http', {}).get('method')
    )
    body = json.loads(event.get('body') or '{}')
    params = event.get('queryStringParameters') or {}

    try:
        if method == 'POST':
            return _create_feedback(body)
        elif method == 'GET':
            return _list_feedback(params)
        elif method == 'DELETE':
            return _delete_feedback(body)
        else:
            return _response(405, {'error': f'Method {method} not allowed'})
    except Exception as e:
        logger.error('HTTP handler error: %s', e, exc_info=True)
        return _response(500, {'error': str(e)})


def _resolve_pk(account_id: str | None) -> str:
    """Return the partition key for the given account scope."""
    if not account_id:
        return GLOBAL_PK
    return f'{ACCOUNT_PK_PREFIX}{account_id}'


def _create_feedback(body: dict):
    """Store a feedback rating for an announcement result.

    Required fields: ``announcement_id``, ``rating`` (``"up"`` or ``"down"``).
    Optional: ``account_id`` — scopes the feedback to a specific account.
    If omitted, the handler attempts to resolve the account_id from the
    most recent result for the announcement.
    """
    announcement_id = (body.get('announcement_id') or '').strip()
    rating = (body.get('rating') or '').strip().lower()

    if not announcement_id:
        return _response(400, {'error': 'announcement_id is required'})
    if rating not in ('up', 'down'):
        return _response(400, {'error': "rating must be 'up' or 'down'"})

    # Resolve account_id: prefer explicit body param, then look up from result
    account_id = (body.get('account_id') or '').strip()
    if not account_id:
        result_item = _fetch_announcement_metadata(announcement_id)
        if result_item:
            account_id = result_item.get('account_id', '')

    pk = _resolve_pk(account_id)
    sk = f'FEEDBACK#{announcement_id}'
    now_iso = _utcnow_iso()

    prefs_table = dynamodb.Table(PREFERENCES_TABLE)
    prefs_table.put_item(Item={
        'pk': pk,
        'sk': sk,
        'announcement_id': announcement_id,
        'rating': rating,
        'account_id': account_id or None,
        'created_at': now_iso,
    })
    logger.info('Stored feedback for %s under pk=%s rating=%s', announcement_id, pk, rating)

    return _response(201, {
        'announcement_id': announcement_id,
        'rating': rating,
        'account_id': account_id or None,
        'created_at': now_iso,
    })


def _list_feedback(params: dict):
    """List feedback records, optionally filtered by account_id."""
    account_id = (params.get('account_id') or '').strip()
    pk = _resolve_pk(account_id)

    prefs_table = dynamodb.Table(PREFERENCES_TABLE)
    result = prefs_table.query(
        KeyConditionExpression='pk = :pk AND begins_with(sk, :prefix)',
        ExpressionAttributeValues={':pk': pk, ':prefix': 'FEEDBACK#'},
    )
    items = [
        {
            'announcement_id': item.get('announcement_id', ''),
            'rating': item.get('rating', ''),
            'account_id': item.get('account_id'),
            'created_at': item.get('created_at', ''),
        }
        for item in result.get('Items', [])
    ]
    return _response(200, items)


def _delete_feedback(body: dict):
    """Delete a feedback record."""
    announcement_id = (body.get('announcement_id') or '').strip()
    if not announcement_id:
        return _response(400, {'error': 'announcement_id is required'})

    account_id = (body.get('account_id') or '').strip()
    pk = _resolve_pk(account_id)
    sk = f'FEEDBACK#{announcement_id}'

    prefs_table = dynamodb.Table(PREFERENCES_TABLE)
    prefs_table.delete_item(Key={'pk': pk, 'sk': sk})
    logger.info('Deleted feedback %s from pk=%s', sk, pk)

    return _response(200, {'deleted': announcement_id})


# ===================================================================
# DynamoDB Stream — Feedback Analyst (used by FeedbackAnalystFunction)
# ===================================================================

def _handle_stream(event):
    """Process DynamoDB Stream records for feedback ratings."""
    records = event.get('Records', [])
    logger.info('Received %d stream record(s)', len(records))

    for record in records:
        try:
            _process_record(record)
        except Exception as e:
            logger.error('Error processing stream record: %s', e, exc_info=True)
            # Fire-and-forget: log errors but never raise
            # The raw rating in DynamoDB is unaffected by any failures here

    return {'statusCode': 200}


def _process_record(record):
    """Process a single DynamoDB Stream record."""
    event_name = record.get('eventName', '')
    if event_name not in ('INSERT', 'MODIFY'):
        logger.debug('Skipping %s event', event_name)
        return

    new_image = record.get('dynamodb', {}).get('NewImage', {})
    if not new_image:
        logger.warning('No NewImage in stream record, skipping')
        return

    # Only process FEEDBACK# records (preferences table is shared)
    sk = new_image.get('sk', {}).get('S', '')
    if not sk.startswith('FEEDBACK#'):
        logger.debug('Skipping non-feedback record: sk=%s', sk)
        return

    announcement_id = new_image.get('announcement_id', {}).get('S', '')
    rating = new_image.get('rating', {}).get('S', '')
    account_id = new_image.get('account_id', {}).get('S', '')

    if not announcement_id or not rating:
        logger.warning('Missing announcement_id or rating in stream record: %s', new_image)
        return

    logger.info(
        'Processing feedback: announcement_id=%s, rating=%s, account_id=%s, event=%s',
        announcement_id, rating, account_id or '(global)', event_name,
    )

    # Fetch announcement metadata from results table
    announcement_data = _fetch_announcement_metadata(announcement_id, account_id=account_id)

    # Load resource inventory from S3
    inventory = _load_inventory_from_s3(account_id=account_id)

    # Build the analyst prompt (now includes account context)
    prompt = _build_analyst_prompt(rating, announcement_data, inventory, account_id=account_id)
    logger.info('Built analyst prompt (%d chars)', len(prompt))

    # Lazy-import AgentCore / Strands dependencies — only the
    # FeedbackAnalystFunction Lambda bundles these packages.
    try:
        from bedrock_agentcore.memory.integrations.strands.config import (
            AgentCoreMemoryConfig,
        )
        from bedrock_agentcore.memory.integrations.strands.session_manager import (
            AgentCoreMemorySessionManager,
        )
        from strands import Agent
        from strands.models import BedrockModel
    except ImportError:
        logger.warning(
            'AgentCore/Strands packages not available — skipping analyst '
            '(this is expected for the HTTP CRUD Lambda)',
        )
        return

    # Configure AgentCore Memory session manager
    # MEMORY_ID may be a full ARN — extract just the name portion
    memory_id = MEMORY_ID
    if memory_id.startswith('arn:'):
        memory_id = memory_id.rsplit('/', 1)[-1]

    # session_id must be alphanumeric + hyphens/underscores, max 100 chars
    short_id = uuid.uuid4().hex[:12]
    session_id = f'fb-{short_id}'
    actor_id = f'awana-{account_id}' if account_id else 'awana'
    config = AgentCoreMemoryConfig(
        memory_id=memory_id,
        session_id=session_id,
        actor_id=actor_id,
    )
    session_manager = AgentCoreMemorySessionManager(config, region_name=region)

    # Load prompt and model ID from S3
    analyst_config = _load_analyst_config()

    # Create and invoke the Feedback Analyst Agent
    agent = Agent(
        model=BedrockModel(model_id=analyst_config['model_id']),
        system_prompt=analyst_config['prompt'],
        session_manager=session_manager,
    )

    logger.info('Invoking Feedback Analyst Agent for %s (session=%s)', announcement_id, session_id)
    agent(prompt)
    logger.info('Feedback Analyst Agent completed for %s', announcement_id)


# ===================================================================
# Shared helpers
# ===================================================================

def _fetch_announcement_metadata(announcement_id: str, account_id: str = ''):
    """Fetch announcement metadata from the results table.

    When *account_id* is provided the query filters for that account's
    sort-key prefix (``<account_id>#``), returning the most relevant result.
    """
    if not RESULTS_TABLE:
        return None
    try:
        results_table = dynamodb.Table(RESULTS_TABLE)

        query_kwargs: dict = {
            'ScanIndexForward': False,
            'Limit': 1,
        }

        if account_id:
            query_kwargs['KeyConditionExpression'] = (
                Key('id').eq(announcement_id)
                & Key('timestamp').begins_with(f'{account_id}#')
            )
        else:
            query_kwargs['KeyConditionExpression'] = Key('id').eq(announcement_id)

        result = results_table.query(**query_kwargs)
        items = result.get('Items', [])
        if items:
            return items[0]
        logger.warning('No announcement metadata found for %s (account=%s)', announcement_id, account_id or 'any')
        return None
    except Exception as e:
        logger.warning('Failed to fetch announcement metadata for %s: %s', announcement_id, e)
        return None


def _load_inventory_from_s3(account_id: str = ''):
    """Load resource inventory snapshot from S3.

    When *account_id* is provided, loads the per-account inventory file
    (``inventory/<account_id>/inventory.json``). Falls back to the legacy
    ``inventory.json`` key when no account-specific file exists.
    """
    if not INVENTORY_BUCKET:
        return None
    try:
        key = f'inventory/{account_id}/inventory.json' if account_id else 'inventory.json'
        response = s3_client.get_object(Bucket=INVENTORY_BUCKET, Key=key)
        return json.loads(response['Body'].read())
    except s3_client.exceptions.NoSuchKey:
        if account_id:
            logger.info('No per-account inventory for %s, trying legacy key', account_id)
            return _load_inventory_from_s3(account_id='')
        return None
    except Exception as e:
        logger.warning('Failed to load inventory: %s', e)
        return None


def _build_analyst_prompt(rating, announcement_data, inventory, account_id: str = ''):
    """Build a multi-dimensional analyst prompt covering service name,
    feature category, and use case — now with account context."""
    direction = 'upvoted (found relevant)' if rating == 'up' else 'downvoted (found not relevant)'

    prompt_parts = [f'A customer has {direction} the following AWS announcement:\n']

    # Account context
    if account_id:
        prompt_parts.append(f'Account context: This feedback is scoped to AWS account {account_id}.')
        prompt_parts.append('')

    # Add announcement metadata if available
    if announcement_data:
        announcement = announcement_data.get('announcement', {})
        if isinstance(announcement, str):
            try:
                announcement = json.loads(announcement)
            except (json.JSONDecodeError, TypeError):
                announcement = {}

        title = announcement.get('title', announcement_data.get('title', 'Unknown'))
        description = announcement.get('description', announcement_data.get('description', ''))
        service = announcement.get('service', announcement_data.get('service', ''))
        category = announcement.get('category', announcement_data.get('category', ''))
        result_val = announcement_data.get('result', '')
        reasoning = announcement_data.get('reasoning', '')

        prompt_parts.append(f'Title: {title}')
        if service:
            prompt_parts.append(f'Service: {service}')
        if category:
            prompt_parts.append(f'Category: {category}')
        if description:
            prompt_parts.append(f'Description: {description}')
        if result_val:
            prompt_parts.append(f'Previous evaluation result: {result_val}')
        if reasoning:
            prompt_parts.append(f'Previous evaluation reasoning: {reasoning}')
    else:
        prompt_parts.append('(Announcement metadata unavailable — analyze based on rating direction only)')

    prompt_parts.append('')

    # Add resource inventory context if available
    if inventory:
        resources = inventory.get('resources', [])
        if resources:
            services_used = sorted(set(r.get('service', '') for r in resources if r.get('service')))
            inventory_label = (
                f"AWS resource inventory for account {account_id}"
                if account_id
                else "Customer's AWS resource inventory"
            )
            prompt_parts.append(f'{inventory_label} (services actively used):')
            for svc in services_used:
                count = sum(1 for r in resources if r.get('service') == svc)
                prompt_parts.append(f'  - {svc} ({count} resource types)')
            prompt_parts.append('')
    else:
        prompt_parts.append('(Customer resource inventory unavailable — analyze without usage context)\n')

    prompt_parts.append(
        'Analyze this feedback across multiple preference dimensions (service name, feature category, '
        'and use case). Provide a nuanced interpretation of what this rating signals about the '
        "customer's preferences."
    )

    return '\n'.join(prompt_parts)


def _load_analyst_config():
    """Load feedback analyst prompt and model ID from S3 prompts manifest."""
    global _analyst_config
    if _analyst_config is not None:
        return _analyst_config

    if not PROMPTS_BUCKET:
        raise ValueError('PROMPTS_BUCKET not set')

    logger.info('Loading feedback analyst config from s3://%s/%s', PROMPTS_BUCKET, PROMPTS_KEY)
    try:
        resp = s3_client.get_object(Bucket=PROMPTS_BUCKET, Key=PROMPTS_KEY)
        manifest = json.loads(resp['Body'].read().decode('utf-8'))
        agents = manifest.get('agents', {})

        if 'feedback-analyst' not in agents:
            raise ValueError('feedback-analyst not found in prompts manifest')

        agent_data = agents['feedback-analyst']
        prompt = agent_data['prompt']
        model_id = agent_data.get('model_id', 'global.anthropic.claude-haiku-4-5-20251001-v1:0')

        _analyst_config = {'prompt': prompt, 'model_id': model_id}
        logger.info('Loaded analyst config: model=%s, prompt length=%d chars', model_id, len(prompt))
        return _analyst_config
    except Exception as e:
        logger.warning('Failed to load analyst config from S3: %s', e, exc_info=True)
        raise


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _response(status_code, body):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET,POST,DELETE,OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type,Authorization',
        },
        'body': json.dumps(body, default=str),
    }
