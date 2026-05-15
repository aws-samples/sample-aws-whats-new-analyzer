"""
Custom Resource Lambda: CORS Updater

Updates API Gateway OPTIONS method integration responses with the correct
CloudFront distribution domain for Access-Control-Allow-Origin headers.

This runs post-deployment to break the circular dependency between
CloudFront Distribution and API Gateway CORS configuration.

NOTE: This handler is invoked by CDK's cr.Provider framework, which handles
the CloudFormation response automatically. Return a dict or raise an exception.
"""

import json
import logging
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def get_all_resources(client, rest_api_id):
    """Retrieve all API Gateway resources, handling pagination."""
    resources = []
    params = {"restApiId": rest_api_id, "limit": 500, "embed": ["methods"]}

    while True:
        response = client.get_resources(**params)
        resources.extend(response.get("items", []))
        position = response.get("position")
        if not position:
            break
        params["position"] = position

    return resources


def update_cors_origins(client, rest_api_id, stage_name, distribution_domain):
    """
    Update OPTIONS method integration responses with the correct
    Access-Control-Allow-Origin value, then create a new deployment
    and update the stage to point to it.
    """
    origin_value = f"'{distribution_domain}'"
    logger.info(
        "Updating CORS origins for API %s stage %s with origin %s",
        rest_api_id,
        stage_name,
        origin_value,
    )

    resources = get_all_resources(client, rest_api_id)
    logger.info("Found %d resources in API %s", len(resources), rest_api_id)

    updated_count = 0
    for resource in resources:
        resource_id = resource["id"]
        resource_methods = resource.get("resourceMethods", {})

        if "OPTIONS" not in resource_methods:
            continue

        logger.info(
            "Updating OPTIONS integration response for resource %s (%s)",
            resource_id,
            resource.get("path", "unknown"),
        )

        try:
            # CDK defaultCorsPreflightOptions uses status code 204 by default
            response = client.update_integration_response(
                restApiId=rest_api_id,
                resourceId=resource_id,
                httpMethod="OPTIONS",
                statusCode="204",
                patchOperations=[
                    {
                        "op": "replace",
                        "path": "/responseParameters/method.response.header.Access-Control-Allow-Origin",
                        "value": origin_value,
                    }
                ],
            )
            logger.info(
                "Updated resource %s, responseParameters: %s",
                resource_id,
                response.get("responseParameters", {}),
            )
            updated_count += 1
        except client.exceptions.NotFoundException:
            logger.warning(
                "Integration response 204 not found for resource %s, trying 200...",
                resource_id,
            )
            # Fallback to status code 200 in case CORS was configured differently
            try:
                client.update_integration_response(
                    restApiId=rest_api_id,
                    resourceId=resource_id,
                    httpMethod="OPTIONS",
                    statusCode="200",
                    patchOperations=[
                        {
                            "op": "replace",
                            "path": "/responseParameters/method.response.header.Access-Control-Allow-Origin",
                            "value": origin_value,
                        }
                    ],
                )
                updated_count += 1
            except client.exceptions.NotFoundException:
                logger.warning(
                    "No integration response found for resource %s OPTIONS method",
                    resource_id,
                )
        except client.exceptions.TooManyRequestsException:
            logger.warning("Throttled by API Gateway, retrying after delay...")
            time.sleep(2)
            try:
                client.update_integration_response(
                    restApiId=rest_api_id,
                    resourceId=resource_id,
                    httpMethod="OPTIONS",
                    statusCode="204",
                    patchOperations=[
                        {
                            "op": "replace",
                            "path": "/responseParameters/method.response.header.Access-Control-Allow-Origin",
                            "value": origin_value,
                        }
                    ],
                )
                updated_count += 1
            except Exception as retry_err:
                logger.error("Retry failed for resource %s: %s", resource_id, str(retry_err))

    logger.info("Updated %d OPTIONS integration responses", updated_count)

    if updated_count > 0:
        # Create a new deployment that captures the updated CORS config
        logger.info("Creating deployment for API %s", rest_api_id)
        deployment_response = client.create_deployment(
            restApiId=rest_api_id,
            description="CORS origin update by custom resource",
        )
        deployment_id = deployment_response["id"]
        logger.info("Created deployment %s", deployment_id)

        # Update the stage to point to our new deployment
        logger.info("Updating stage %s to deployment %s", stage_name, deployment_id)
        client.update_stage(
            restApiId=rest_api_id,
            stageName=stage_name,
            patchOperations=[
                {
                    "op": "replace",
                    "path": "/deploymentId",
                    "value": deployment_id,
                }
            ],
        )
        logger.info("Stage %s now points to deployment %s", stage_name, deployment_id)
    else:
        logger.warning("No OPTIONS methods found to update!")

    return updated_count


def handler(event, context):
    """
    CDK cr.Provider onEvent handler.

    The Provider framework handles CloudFormation responses automatically.
    Return a dict on success or raise an exception on failure.
    """
    logger.info("Received event: %s", json.dumps(event, default=str))

    request_type = event.get("RequestType", "")
    properties = event.get("ResourceProperties", {})

    rest_api_id = properties.get("RestApiId", "")
    stage_name = properties.get("StageName", "")
    distribution_domain = properties.get("DistributionDomain", "")

    if request_type in ("Create", "Update"):
        if not rest_api_id or not stage_name or not distribution_domain:
            raise ValueError(
                f"Missing required properties: RestApiId={rest_api_id}, "
                f"StageName={stage_name}, DistributionDomain={distribution_domain}"
            )

        client = boto3.client("apigateway")
        updated = update_cors_origins(
            client, rest_api_id, stage_name, distribution_domain
        )
        logger.info(
            "Successfully updated %d OPTIONS responses for API %s",
            updated,
            rest_api_id,
        )

        return {
            "PhysicalResourceId": f"cors-updater-{rest_api_id}",
            "Data": {
                "UpdatedCount": str(updated),
                "DistributionDomain": distribution_domain,
            },
        }

    elif request_type == "Delete":
        logger.info("Delete request - no action needed (API cleanup handles CORS)")
        return {
            "PhysicalResourceId": event.get("PhysicalResourceId", f"cors-updater-{rest_api_id}"),
        }

    else:
        logger.warning("Unknown request type: %s", request_type)
        return {
            "PhysicalResourceId": event.get("PhysicalResourceId", f"cors-updater-{rest_api_id}"),
        }
