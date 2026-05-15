# ─────────────────────────────────────────────────────────────────────────────
# SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
# This code is provided as a reference implementation only.
# ─────────────────────────────────────────────────────────────────────────────

import json
import boto3
import os
import logging
from datetime import datetime, timezone

log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

dynamodb = boto3.resource('dynamodb')

DEDUP_TABLE_NAME = os.environ['DEDUP_TABLE_NAME']

# Must match the constant in ../index.py
STATE_ITEM_ID = '__crawler_state__'

dedup_table = dynamodb.Table(DEDUP_TABLE_NAME)


def lambda_handler(event, context):
    logger.info(f"DLQ handler invoked with {len(event['Records'])} record(s)")

    for record in event['Records']:
        message = json.loads(record['body'])
        item_id = message.get('id')
        pub_date_str = message.get('pubDate')

        if not item_id or not pub_date_str:
            logger.error(f"Message missing 'id' or 'pubDate', skipping: {json.dumps(message)[:200]}")
            continue

        logger.info(f"Processing DLQ message: id={item_id}, pubDate={pub_date_str}")

        # Delete the item from the deduplication table so the next crawl
        # can pick it up again.
        try:
            dedup_table.delete_item(Key={'id': item_id})
            logger.info(f"Deleted dedup entry for id={item_id}")
        except Exception as e:
            logger.warning(f"Failed to delete dedup entry for id={item_id}: {e}", exc_info=True)
            raise

        # Roll back the crawler state row only if the failed message's pubDate
        # is older than the currently stored value. This makes sure we re-crawl
        # at least as far back as the failed item.
        try:
            msg_pub_date = datetime.fromisoformat(pub_date_str.replace('Z', '+00:00'))

            current_value = _get_current_last_pub_date()
            if current_value is None or msg_pub_date < current_value:
                _set_last_pub_date(msg_pub_date)
                logger.info(f"Reset last pub date to {pub_date_str} (was {current_value})")
            else:
                logger.info(
                    f"Skipping state-row update: message pubDate {pub_date_str} "
                    f"is not older than current value {current_value.isoformat()}"
                )
        except Exception as e:
            logger.warning(f"Failed to update crawler state row: {e}", exc_info=True)
            raise

    logger.info("DLQ handler completed")
    return {'statusCode': 200}


def _get_current_last_pub_date():
    resp = dedup_table.get_item(Key={'id': STATE_ITEM_ID})
    item = resp.get('Item')
    if item is None:
        logger.warning("State row not found in dedup table, will update unconditionally")
        return None
    value = item.get('lastPubDate')
    if not value:
        return None
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _set_last_pub_date(dt):
    iso = dt.isoformat().replace('+00:00', 'Z')
    updated_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    dedup_table.put_item(Item={
        'id': STATE_ITEM_ID,
        'lastPubDate': iso,
        'updatedAt': updated_at,
    })
