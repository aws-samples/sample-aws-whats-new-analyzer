# ─────────────────────────────────────────────────────────────────────────────
# SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
# This code is provided as a reference implementation only.
# ─────────────────────────────────────────────────────────────────────────────

import json
import os
import logging
import time
from datetime import datetime, timezone
from urllib.request import urlopen
import defusedxml.ElementTree as ElementTree

import boto3

log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

dynamodb = boto3.resource('dynamodb')
sqs = boto3.client('sqs')

RSS_URL = 'https://aws.amazon.com/about-aws/whats-new/recent/feed/'
DEDUP_TABLE_NAME = os.environ['DEDUP_TABLE_NAME']
QUEUE_URL = os.environ['QUEUE_URL']
DEDUP_TTL_DAYS = 30

# Sentinel row in the dedup table that holds crawler state (last seen pubDate).
# The row has no `ttl` attribute on purpose — DynamoDB TTL only evicts items
# whose `ttl` is set and in the past, so this row persists indefinitely.
STATE_ITEM_ID = '__crawler_state__'
EPOCH_ISO = '1970-01-01T00:00:00Z'

dedup_table = dynamodb.Table(DEDUP_TABLE_NAME)


def fetch_rss(url):
    logger.info(f'Fetching RSS feed from {url}')
    if not url.startswith('https://'):
        raise ValueError(f'Refusing to fetch non-HTTPS URL: {url}')
    with urlopen(url) as resp:
        data = resp.read()
        logger.debug(f'RSS response size: {len(data)} bytes')
        return data


def parse_items(xml_data):
    root = ElementTree.fromstring(xml_data)
    items = []
    for item_el in root.findall('.//item'):
        guid_el = item_el.find('guid')
        item_id = guid_el.text if guid_el is not None else None
        if not item_id:
            continue

        pub_date_str = (item_el.findtext('pubDate') or '').strip()
        pub_date = _parse_rss_date(pub_date_str)

        items.append({
            'id': item_id,
            'category': item_el.findtext('category') or '',
            'pubDate': pub_date.isoformat().replace('+00:00', 'Z'),
            'title': item_el.findtext('title') or '',
            'description': item_el.findtext('description') or '',
            'link': item_el.findtext('link') or '',
            'author': item_el.findtext('author') or '',
        })
    return items


def _parse_rss_date(date_str):
    """Parse RFC 2822 date from RSS feed."""
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(date_str).astimezone(timezone.utc)
    except Exception:
        logger.warning(f'Failed to parse date: {date_str}, using epoch')
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def is_duplicate(item_id):
    result = dedup_table.get_item(Key={'id': item_id})
    dup = 'Item' in result
    logger.debug(f'Dedup check for {item_id}: {"duplicate" if dup else "new"}')
    return dup


def mark_as_seen(item_id, link):
    ttl = int(time.time()) + DEDUP_TTL_DAYS * 86400
    crawled_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    dedup_table.put_item(Item={'id': item_id, 'ttl': ttl, 'link': link, 'crawledAt': crawled_at})
    logger.debug(f'Marked as seen: {item_id} (TTL: {ttl}, link: {link})')


def send_to_queue(item):
    logger.debug(f'Sending to SQS: {item["id"]}')
    sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps(item))


def get_last_pub_date():
    """Read the last seen pubDate from the dedup-table state row.

    If the state row is missing (first invocation, fresh deploy), create it
    with an epoch value so subsequent runs always find a row to update.
    """
    resp = dedup_table.get_item(Key={'id': STATE_ITEM_ID})
    item = resp.get('Item')

    if item is None:
        logger.info('State row not found, initializing with epoch')
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        store_last_pub_date(epoch)
        return epoch

    value = item.get('lastPubDate', EPOCH_ISO)
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    logger.info(f'Last publication date from state row: {dt.isoformat()}')
    return dt


def store_last_pub_date(dt):
    """Upsert the dedup-table state row with the new lastPubDate.

    The state row is intentionally written without a `ttl` attribute so that
    DynamoDB TTL never evicts it.
    """
    iso = dt.isoformat().replace('+00:00', 'Z')
    updated_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    logger.info(f'Updating last publication date to {iso}')
    dedup_table.put_item(Item={
        'id': STATE_ITEM_ID,
        'lastPubDate': iso,
        'updatedAt': updated_at,
    })


def lambda_handler(event, context):
    logger.info('Crawler invoked')

    xml_data = fetch_rss(RSS_URL)
    all_items = parse_items(xml_data)
    last_date = get_last_pub_date()

    items = [
        item for item in all_items
        if datetime.fromisoformat(item['pubDate'].replace('Z', '+00:00')) >= last_date
    ]
    logger.info(f'Parsed {len(all_items)} total items, {len(items)} newer than {last_date.isoformat()}')

    new_count = 0
    for item in items:
        if is_duplicate(item['id']):
            logger.debug(f'Skipping duplicate: {item["id"]}')
            continue
        send_to_queue(item)
        mark_as_seen(item['id'], item['link'])
        new_count += 1
        logger.info(f'Queued: {item["title"]}')

    if items:
        latest_date = max(
            datetime.fromisoformat(item['pubDate'].replace('Z', '+00:00'))
            for item in items
        )
        store_last_pub_date(latest_date)

    logger.info(f'Done. {new_count} new items queued, {len(items) - new_count} duplicates skipped.')
    return {'statusCode': 200, 'body': f'Queued {new_count} new announcements'}
