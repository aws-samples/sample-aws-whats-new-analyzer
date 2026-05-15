"""
Lambda handler for customer preferences CRUD operations.

SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
This code is provided as a reference implementation only.

Sits behind API Gateway with Cognito auth.

Preferences are stored in DynamoDB with account-scoped partition keys:
  - ACCOUNT#GLOBAL  — preferences that apply to all accounts (default)
  - ACCOUNT#<account_id> — preferences scoped to a specific 12-digit account

The optional ``account_id`` query/body parameter controls the scope.
When absent, operations target the global scope (ACCOUNT#GLOBAL).
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import boto3

log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

TABLE_NAME = os.environ['PREFERENCES_TABLE']
GLOBAL_PK = 'ACCOUNT#GLOBAL'
ACCOUNT_PK_PREFIX = 'ACCOUNT#'
ACCOUNT_ID_PATTERN = re.compile(r'^\d{12}$')

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(TABLE_NAME)


def _resolve_pk(account_id: str | None) -> str:
    """Return the partition key for the given account scope.

    If *account_id* is ``None`` or empty the global scope is used.
    A non-empty *account_id* must be a valid 12-digit AWS account ID.
    """
    if not account_id:
        return GLOBAL_PK
    return f'{ACCOUNT_PK_PREFIX}{account_id}'


def _validate_account_id(account_id: str | None) -> str | None:
    """Validate an optional account_id parameter.

    Returns ``None`` when the value is acceptable (empty or valid 12-digit).
    Returns an error message string when validation fails.
    """
    if not account_id:
        return None
    if not ACCOUNT_ID_PATTERN.match(account_id):
        return 'account_id must be exactly 12 digits'
    return None


def lambda_handler(event, context):
    method = (
        event.get('httpMethod')
        or event.get('requestContext', {}).get('http', {}).get('method')
    )
    body = json.loads(event.get('body') or '{}')
    params = event.get('queryStringParameters') or {}

    try:
        if method == 'GET':
            return list_preferences(params)
        elif method == 'POST':
            return create_preference(body)
        elif method == 'PUT':
            return update_preference(body)
        elif method == 'DELETE':
            return delete_preference(body)
        else:
            return response(405, {'error': f'Method {method} not allowed'})
    except Exception as e:
        logger.error('Unhandled error: %s', e, exc_info=True)
        return response(500, {'error': str(e)})


def list_preferences(params: dict):
    """List preferences, optionally filtered by account scope.

    Query parameters:
      - ``account_id`` — return preferences for this specific account.
      - ``scope``      — ``"all"`` to return both global and account-scoped
                          preferences (requires ``account_id``).
                          Default behaviour returns only the targeted scope.
    """
    account_id = (params.get('account_id') or '').strip()
    scope = (params.get('scope') or '').strip().lower()

    error = _validate_account_id(account_id)
    if error:
        return response(400, {'error': error})

    items: list[dict] = []

    if scope == 'all' and account_id:
        # Return both global AND account-scoped preferences
        logger.info('Listing preferences for global + account %s', account_id)
        for pk in (GLOBAL_PK, _resolve_pk(account_id)):
            result = table.query(
                KeyConditionExpression='pk = :pk AND begins_with(sk, :prefix)',
                ExpressionAttributeValues={':pk': pk, ':prefix': 'PREF#'},
            )
            for item in result.get('Items', []):
                items.append(_format_item(item))
    else:
        pk = _resolve_pk(account_id)
        logger.info('Listing preferences for pk=%s', pk)
        result = table.query(
            KeyConditionExpression='pk = :pk AND begins_with(sk, :prefix)',
            ExpressionAttributeValues={':pk': pk, ':prefix': 'PREF#'},
        )
        items = [_format_item(item) for item in result.get('Items', [])]

    return response(200, items)


def create_preference(body: dict):
    statement = (body.get('statement') or '').strip()
    if not statement:
        return response(400, {'error': 'statement is required'})

    account_id = (body.get('account_id') or '').strip()
    error = _validate_account_id(account_id)
    if error:
        return response(400, {'error': error})

    pk = _resolve_pk(account_id)
    sk = f'PREF#{uuid.uuid4()}'
    now = datetime.now(timezone.utc).isoformat()

    item = {
        'pk': pk,
        'sk': sk,
        'statement': statement,
        'created_at': now,
    }
    if account_id:
        item['account_id'] = account_id

    table.put_item(Item=item)
    logger.info('Created preference %s under pk=%s', sk, pk)

    return response(201, {
        'id': sk,
        'statement': statement,
        'created_at': now,
        'account_id': account_id or None,
    })


def update_preference(body: dict):
    pref_id = (body.get('id') or '').strip()
    statement = (body.get('statement') or '').strip()
    if not pref_id or not statement:
        return response(400, {'error': 'id and statement are required'})

    account_id = (body.get('account_id') or '').strip()
    error = _validate_account_id(account_id)
    if error:
        return response(400, {'error': error})

    pk = _resolve_pk(account_id)

    table.update_item(
        Key={'pk': pk, 'sk': pref_id},
        UpdateExpression='SET statement = :s, updated_at = :u',
        ExpressionAttributeValues={
            ':s': statement,
            ':u': datetime.now(timezone.utc).isoformat(),
        },
        ConditionExpression='attribute_exists(pk)',
    )
    logger.info('Updated preference %s under pk=%s', pref_id, pk)

    return response(200, {'id': pref_id, 'statement': statement})


def delete_preference(body: dict):
    pref_id = (body.get('id') or '').strip()
    if not pref_id:
        return response(400, {'error': 'id is required'})

    account_id = (body.get('account_id') or '').strip()
    error = _validate_account_id(account_id)
    if error:
        return response(400, {'error': error})

    pk = _resolve_pk(account_id)

    table.delete_item(Key={'pk': pk, 'sk': pref_id})
    logger.info('Deleted preference %s from pk=%s', pref_id, pk)

    return response(200, {'deleted': pref_id})


def _format_item(item: dict) -> dict:
    """Format a DynamoDB item for the API response."""
    pk = item.get('pk', '')
    account_id = None
    if pk.startswith(ACCOUNT_PK_PREFIX) and pk != GLOBAL_PK:
        account_id = pk[len(ACCOUNT_PK_PREFIX):]

    return {
        'id': item['sk'],
        'statement': item['statement'],
        'created_at': item.get('created_at', ''),
        'updated_at': item.get('updated_at'),
        'account_id': account_id,
    }


def response(status_code, body):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type,Authorization',
        },
        'body': json.dumps(body),
    }
