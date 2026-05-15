"""
Lambda handler for fetching announcement processing results.

SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
This code is provided as a reference implementation only.

Sits behind API Gateway with Cognito auth.

The Results Table sort key format is ``<account_id>#<ISO8601_timestamp>``.
Chronological reads use the ``ByPubDate`` GSI:

  * partition key: ``gsi_pk`` (constant ``"ALL"``)
  * sort key:      ``pubDate`` (RSS publish date, ISO 8601)

The single-announcement query path (``?id=...``) still queries the base table
by ``id`` — it returns all per-account rows for that announcement.
"""

import base64
import json
import os
import logging

import boto3
from boto3.dynamodb.conditions import Key, Attr

logger = logging.getLogger()
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO'))

TABLE_NAME = os.environ['RESULTS_TABLE']
GSI_NAME = 'ByPubDate'
GSI_PK_VALUE = 'ALL'

# Maximum number of paginated GSI Query calls per request. Bounds worst-case
# latency when a strict FilterExpression makes most pages near-empty.
MAX_PAGE_FETCHES = 10
# Hard upper bound on items returned per response, regardless of ?limit=.
MAX_PAGE_SIZE = 100
# Default page size if the caller doesn't specify ?limit=.
DEFAULT_PAGE_SIZE = 30

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(TABLE_NAME)


def _extract_timestamp(sort_key):
    """Extract the actual timestamp portion from a sort key.

    Sort key format: ``<account_id>#<ISO8601_timestamp>``
    Falls back to the raw value when no ``#`` separator is present
    (backward-compatible with legacy data).
    """
    if '#' in str(sort_key):
        return sort_key.split('#', 1)[1]
    return sort_key


def _encode_cursor(last_evaluated_key):
    """Encode a DynamoDB LastEvaluatedKey as a URL-safe opaque cursor."""
    if not last_evaluated_key:
        return None
    return base64.urlsafe_b64encode(
        json.dumps(last_evaluated_key, default=str).encode('utf-8')
    ).decode('ascii')


def _decode_cursor(cursor):
    """Decode an opaque cursor back into a DynamoDB ExclusiveStartKey."""
    if not cursor:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode('ascii')).decode('utf-8'))
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        logger.warning(f"Invalid cursor, ignoring: {e}")
        return None


def lambda_handler(event, context):
    method = event.get('httpMethod') or event.get('requestContext', {}).get('http', {}).get('method')
    params = event.get('queryStringParameters') or {}

    try:
        if method == 'GET':
            return get_results(params)
        else:
            return response(405, {'error': f'Method {method} not allowed'})
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return response(500, {'error': str(e)})


def get_results(params):
    announcement_id = params.get('id')
    account_id = params.get('account_id')
    is_relevant = params.get('is_relevant')

    try:
        page_size = max(1, min(int(params.get('limit', str(DEFAULT_PAGE_SIZE))), MAX_PAGE_SIZE))
    except (TypeError, ValueError):
        page_size = DEFAULT_PAGE_SIZE

    # Single-announcement lookup: query base table by id.
    # Returns all per-account rows for the announcement; no pagination.
    if announcement_id:
        return _get_single_announcement(announcement_id, account_id, is_relevant, page_size)

    # Listing path: paginate the ByPubDate GSI newest-first.
    return _list_paginated(account_id, is_relevant, page_size, params.get('cursor'))


def _get_single_announcement(announcement_id, account_id, is_relevant, page_size):
    query_kwargs = {
        'ScanIndexForward': False,
        'Limit': page_size,
    }

    if account_id:
        logger.info(f"Querying announcement '{announcement_id}' for account '{account_id}'")
        query_kwargs['KeyConditionExpression'] = (
            Key('id').eq(announcement_id)
            & Key('timestamp').begins_with(f'{account_id}#')
        )
    else:
        query_kwargs['KeyConditionExpression'] = Key('id').eq(announcement_id)

    if is_relevant is not None:
        query_kwargs['FilterExpression'] = Attr('is_relevant').eq(is_relevant.lower() == 'true')

    result = table.query(**query_kwargs)
    items = result.get('Items', [])
    return response(200, {'items': items, 'cursor': None})


def _list_paginated(account_id, is_relevant, page_size, cursor):
    """Query the ByPubDate GSI and accumulate up to ``page_size`` matching items.

    Filters (``account_id``, ``is_relevant``) are applied via FilterExpression,
    which runs after items are read but before they're returned. To avoid
    short pages when filters are strict, we loop Query calls until we have
    enough items, hit the end, or reach ``MAX_PAGE_FETCHES``.

    The returned cursor lets the client resume exactly where this call stopped.
    """
    filter_expressions = []
    if account_id:
        filter_expressions.append(Attr('account_id').eq(account_id))
    if is_relevant is not None:
        filter_expressions.append(Attr('is_relevant').eq(is_relevant.lower() == 'true'))

    combined_filter = None
    if filter_expressions:
        combined_filter = filter_expressions[0]
        for expr in filter_expressions[1:]:
            combined_filter = combined_filter & expr

    collected = []
    next_start_key = _decode_cursor(cursor)
    fetches = 0

    while len(collected) < page_size and fetches < MAX_PAGE_FETCHES:
        query_kwargs = {
            'IndexName': GSI_NAME,
            'KeyConditionExpression': Key('gsi_pk').eq(GSI_PK_VALUE),
            'ScanIndexForward': False,  # newest first
            'Limit': page_size,
        }
        if combined_filter is not None:
            query_kwargs['FilterExpression'] = combined_filter
        if next_start_key:
            query_kwargs['ExclusiveStartKey'] = next_start_key

        result = table.query(**query_kwargs)
        collected.extend(result.get('Items', []))
        next_start_key = result.get('LastEvaluatedKey')
        fetches += 1

        if not next_start_key:
            break  # exhausted the GSI

    # Trim to the requested page size; remember where we left off so the next
    # call resumes from item N+1, not from the start of the next DynamoDB page.
    if len(collected) > page_size:
        # We over-collected on the last query. Re-encode a synthetic cursor
        # pointing at the last item we're returning.
        truncate_at = page_size
        last_returned = collected[truncate_at - 1]
        collected = collected[:truncate_at]
        next_start_key = {
            'gsi_pk': GSI_PK_VALUE,
            'pubDate': last_returned.get('pubDate', ''),
            'id': last_returned.get('id', ''),
            'timestamp': last_returned.get('timestamp', ''),
        }

    return response(200, {
        'items': collected,
        'cursor': _encode_cursor(next_start_key),
    })


def response(status_code, body):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET,OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type,Authorization',
        },
        'body': json.dumps(body, default=str),
    }
