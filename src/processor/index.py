# ─────────────────────────────────────────────────────────────────────────────
# SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
# This code is provided as a reference implementation only.
# ─────────────────────────────────────────────────────────────────────────────

import json
import boto3
import os
import uuid
import logging
from datetime import datetime, timezone
from botocore.exceptions import BotoCoreError, ClientError

# Errors that indicate persistent infrastructure problems — no point retrying
CIRCUIT_BREAKER_ERROR_CODES = {
    'ResourceNotFoundException',   # Agent runtime deleted or ARN wrong
    'AccessDeniedException',       # IAM permissions revoked
    'ServiceUnavailableException', # AgentCore down in region
}

log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

agent_core_client = boto3.client('bedrock-agentcore')
sns = boto3.client('sns')
lambda_client = boto3.client('lambda')
dynamodb = boto3.resource('dynamodb')

agent_runtime_arn = os.environ.get('AGENT_RUNTIME_ARN', '')

topic_arn = os.environ['TOPIC_ARN']
alerts_topic_arn = os.environ.get('ALERTS_TOPIC_ARN', '')
processor_function_name = os.environ.get('PROCESSOR_FUNCTION_NAME', '')
central_account_id = os.environ.get('CENTRAL_ACCOUNT_ID', '')
results_table = dynamodb.Table(os.environ['RESULTS_TABLE'])


def trip_circuit_breaker(error_msg: str):
    """Disable the SQS event source mapping and send an alert."""
    logger.error(f"CIRCUIT BREAKER TRIPPED: {error_msg}")

    # Find and disable the SQS event source mapping for this function
    if processor_function_name:
        try:
            mappings = lambda_client.list_event_source_mappings(
                FunctionName=processor_function_name
            )
            for mapping in mappings.get('EventSourceMappings', []):
                if 'sqs' in mapping.get('EventSourceArn', '').lower():
                    lambda_client.update_event_source_mapping(
                        UUID=mapping['UUID'],
                        Enabled=False,
                    )
                    logger.info(f"Disabled SQS event source mapping {mapping['UUID']}")
        except Exception as e:
            logger.error(f"Failed to disable event source mapping: {e}", exc_info=True)

    # Send alert via SNS
    if alerts_topic_arn:
        try:
            sns.publish(
                TopicArn=alerts_topic_arn,
                Subject='Awana Circuit Breaker Tripped',
                Message=(
                    f"The Awana processor circuit breaker has been tripped.\n\n"
                    f"Reason: {error_msg}\n\n"
                    f"The SQS event source has been disabled to prevent further failures.\n"
                    f"Messages will accumulate in the queue until the issue is resolved.\n\n"
                    f"To re-enable processing after fixing the issue:\n"
                    f"  aws lambda list-event-source-mappings "
                    f"--function-name {processor_function_name} --query "
                    f"\"EventSourceMappings[?contains(EventSourceArn,'sqs')].UUID\" --output text\n"
                    f"  aws lambda update-event-source-mapping --uuid <UUID> --enabled\n\n"
                    f"Timestamp: {datetime.now(timezone.utc).isoformat()}"
                ),
            )
            logger.info("Circuit breaker alert sent to SNS")
        except Exception as e:
            logger.error(f"Failed to send circuit breaker alert: {e}", exc_info=True)


def lambda_handler(event, context):
    logger.info(f"Processing {len(event['Records'])} messages")

    for record in event['Records']:
        message = json.loads(record['body'])
        announcement_id = message.get('link', message.get('id', 'unknown'))

        logger.info(f"Processing announcement: {announcement_id}")

        if not agent_runtime_arn:
            logger.warning("AGENT_RUNTIME_ARN not set — skipping agent invocation for %s", announcement_id)
            continue

        try:
            payload = json.dumps({"input": {"prompt": json.dumps(message)}}).encode()
            session_id = str(uuid.uuid4())

            logger.info(f"Invoking agent for {announcement_id} with session {session_id}")
            response = agent_core_client.invoke_agent_runtime(
                agentRuntimeArn=agent_runtime_arn,
                runtimeSessionId=session_id,
                payload=payload
            )

            result = []
            if response.get('contentType') == 'application/json':
                for chunk in response.get('response', []):
                    result.append(chunk.decode('utf-8'))

            result_text = ''.join(result)
            logger.info(f"Agent response for {announcement_id}: {result_text[:200]}...")

            # Parse the structured JSON response from the agent
            try:
                agent_output = json.loads(result_text)
                if 'output' in agent_output:
                    agent_output = agent_output['output']
                agent_result = agent_output.get('result', '')
                agent_reasoning = agent_output.get('reasoning', '')
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"Could not parse structured output for {announcement_id}, falling back to raw text")
                agent_result = result_text
                agent_reasoning = ''

            is_relevant = agent_result.lower().strip() == 'relevant'

            # Resolve account_id: prefer message payload, fall back to CENTRAL_ACCOUNT_ID
            account_id = message.get('account_id', '') or central_account_id
            group_fingerprint = message.get('group_fingerprint', '') or ''
            timestamp = datetime.now(timezone.utc).isoformat()

            try:
                results_table.put_item(Item={
                    'id': announcement_id,
                    'timestamp': f"{account_id}#{timestamp}",
                    'account_id': account_id,
                    'group_fingerprint': group_fingerprint,
                    'announcement': message,
                    'agent_response': result_text,
                    'result': agent_result,
                    'reasoning': agent_reasoning,
                    'is_relevant': is_relevant,
                    'ttl': int(datetime.now(timezone.utc).timestamp()) + 365 * 86400,
                })
            except ClientError as e:
                error_code = e.response.get('Error', {}).get('Code', '')
                if error_code in ('ResourceNotFoundException', 'AccessDeniedException'):
                    trip_circuit_breaker(f"DynamoDB {error_code}: {e}")
                raise

            logger.info(f"Stored result for {announcement_id} (relevant: {is_relevant})")

            if is_relevant:
                sns.publish(
                    TopicArn=topic_arn,
                    Message=json.dumps(message),
                    Subject=f"Relevant: {message.get('title', 'AWS Announcement')}"[:100]
                )
                logger.info(f"Published relevant announcement to SNS: {announcement_id}")

        except agent_core_client.exceptions.RuntimeClientError as e:
            error_msg = f"Agent runtime error for {announcement_id}: {e}"
            logger.error(error_msg, exc_info=True)
            trip_circuit_breaker(error_msg)
            raise

        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', '')
            http_status = e.response.get('ResponseMetadata', {}).get('HTTPStatusCode', 0)
            error_msg = f"Error processing {announcement_id}: [{error_code}] {e}"
            logger.error(error_msg, exc_info=True)

            if error_code in CIRCUIT_BREAKER_ERROR_CODES:
                trip_circuit_breaker(f"Agent {error_code}: {e}")
            elif http_status >= 500:
                trip_circuit_breaker(f"Agent returned HTTP {http_status}: {e}")

            raise

        except BotoCoreError as e:
            error_msg = f"Agent infrastructure failure ({type(e).__name__}): {e}"
            logger.error(error_msg, exc_info=True)
            trip_circuit_breaker(error_msg)
            raise

        except Exception as e:
            error_msg = f"Error processing announcement {announcement_id}: {e}"
            logger.error(error_msg, exc_info=True)
            raise

    logger.info("Batch processing completed")
    return {'statusCode': 200}
