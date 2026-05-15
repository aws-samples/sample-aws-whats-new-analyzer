"""
Awana Agent Server — evaluates AWS announcements for customer relevance.

SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
This code is provided as a reference implementation only.

Invocation payload:
{
    "input": {"prompt": "...", "mode": "pre-filter|service-routing|evaluate", "account_group": {...}}
}
"""

import logging
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from strands import Agent
from strands.models import BedrockModel
import boto3

# ─── Logging ───

log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    force=True,
)
logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, log_level, logging.INFO))

# ─── AWS clients ───

region = os.environ.get('AWS_REGION')
s3_client = boto3.client('s3', region_name=region)
_dynamodb_resource = boto3.resource('dynamodb', region_name=region)

# ─── Environment ───

INVENTORY_BUCKET = os.environ.get('INVENTORY_BUCKET', '')
PROMPTS_BUCKET = os.environ.get('PROMPTS_BUCKET', '')
PROMPTS_KEY = os.environ.get('PROMPTS_KEY', 'config/prompts.json')
PREFERENCES_TABLE = os.environ.get('PREFERENCES_TABLE', '')
ENABLE_MEMORY = os.environ.get('ENABLE_MEMORY', 'false').lower() == 'true'
MEMORY_ID = os.environ.get('MEMORY_ID', '')

CONTEXT_FILE_KEY = 'inventory/consolidated-context.json'
CONTEXT_CACHE_TTL_SECONDS = 300
GLOBAL_PREFERENCES_PK = 'ACCOUNT#GLOBAL'

# ─── Structured output models ───


class AnnouncementRelevance(BaseModel):
    result: str = Field(description="'relevant', 'not relevant', or 'not my scope'")
    reasoning: str = Field(description="Brief explanation of the decision")


class PreFilterResult(BaseModel):
    services: list[str] = Field(description="AWS service names referenced (e.g. 'Amazon S3')")
    database_engines: list[str] = Field(description="Database engines referenced (e.g. 'PostgreSQL')")
    cache_engines: list[str] = Field(description="Cache engines referenced (e.g. 'Redis', 'Memcached', 'Valkey')")
    ec2_platforms: list[str] = Field(description="EC2 families or platforms referenced (e.g. 'Graviton', 'm7g')")
    regions: list[str] = Field(description="AWS regions referenced (canonical codes, e.g. 'eu-west-1')")
    passes: bool = Field(description="True if any reference matches the org inventory")
    reason: str = Field(description="Brief explanation")


class ServiceRoutingResult(BaseModel):
    route: str = Field(description="'single_service' or 'multi_service'")
    matched_service: str = Field(description="Matched service name from the list, or empty string")
    services: list[str] = Field(description="All AWS services identified in the announcement")


# ─── Request/Response models ───


class InvocationRequest(BaseModel):
    input: Dict[str, Any]


class InvocationResponse(BaseModel):
    output: Dict[str, Any]


# ─── Consolidated context cache ───

_context_cache: Optional[Dict[str, Any]] = None
_context_cache_timestamp: float = 0.0


def load_consolidated_context() -> Optional[Dict[str, Any]]:
    """Load consolidated-context.json from S3 with TTL cache."""
    global _context_cache, _context_cache_timestamp

    now = time.monotonic()
    if _context_cache is not None and (now - _context_cache_timestamp) < CONTEXT_CACHE_TTL_SECONDS:
        return _context_cache

    if not INVENTORY_BUCKET:
        logger.warning("INVENTORY_BUCKET not set")
        return None

    try:
        resp = s3_client.get_object(Bucket=INVENTORY_BUCKET, Key=CONTEXT_FILE_KEY)
        context = json.loads(resp['Body'].read().decode('utf-8'))
        _context_cache = context
        _context_cache_timestamp = now
        logger.info("Loaded consolidated context from S3")
        return context
    except Exception as e:
        logger.warning("Failed to load consolidated context: %s", e)
        return None


# ─── S3 prompts loading ───

_prompts_cache: Optional[Dict[str, Any]] = None
_prompts_cache_timestamp: float = 0.0
PROMPTS_CACHE_TTL_SECONDS = 300


def _load_prompts_manifest() -> Dict[str, Any]:
    """Load prompts.json from S3 with TTL cache."""
    global _prompts_cache, _prompts_cache_timestamp

    now = time.monotonic()
    if _prompts_cache is not None and (now - _prompts_cache_timestamp) < PROMPTS_CACHE_TTL_SECONDS:
        return _prompts_cache

    if not PROMPTS_BUCKET:
        raise ValueError("PROMPTS_BUCKET not set")

    try:
        resp = s3_client.get_object(Bucket=PROMPTS_BUCKET, Key=PROMPTS_KEY)
        manifest = json.loads(resp['Body'].read().decode('utf-8'))
        _prompts_cache = manifest
        _prompts_cache_timestamp = now
        logger.info("Loaded prompts manifest from s3://%s/%s", PROMPTS_BUCKET, PROMPTS_KEY)
        return manifest
    except Exception as e:
        # If cache exists but is stale, return stale data rather than failing
        if _prompts_cache is not None:
            logger.warning("Failed to refresh prompts, using stale cache: %s", e)
            return _prompts_cache
        raise ValueError(f"Failed to load prompts from s3://{PROMPTS_BUCKET}/{PROMPTS_KEY}") from e


def load_system_prompt() -> str:
    """Load the shared system prompt from the prompts manifest."""
    manifest = _load_prompts_manifest()
    return manifest['system_prompt']


def load_agent_config(agent_name: str) -> dict:
    """Load agent prompt and model ID from the prompts manifest."""
    manifest = _load_prompts_manifest()
    agents = manifest.get('agents', {})

    if agent_name not in agents:
        raise ValueError(f"Agent '{agent_name}' not found in prompts manifest")

    agent_data = agents[agent_name]
    return {
        'prompt': agent_data['prompt'],
        'model_id': agent_data.get('model_id', 'global.anthropic.claude-haiku-4-5-20251001-v1:0'),
    }


# ─── Helpers ───


def parse_announcement(prompt_text: str) -> Optional[Dict[str, str]]:
    """Parse announcement from prompt text (JSON or plain text)."""
    if not prompt_text or not isinstance(prompt_text, str):
        return None
    prompt_text = prompt_text.strip()
    if not prompt_text:
        return None

    try:
        data = json.loads(prompt_text)
        if isinstance(data, dict):
            title = str(data.get('title', ''))
            description = str(data.get('description', ''))
            if title or description:
                return {'title': title, 'description': description}
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    return {'title': '', 'description': prompt_text}


def _fetch_preferences(account_id: str = '') -> str:
    """Fetch preferences from DynamoDB, formatted for prompt injection."""
    if not PREFERENCES_TABLE:
        return "(no preferences configured)"

    table = _dynamodb_resource.Table(PREFERENCES_TABLE)
    preferences: list[str] = []

    try:
        resp = table.query(
            KeyConditionExpression='pk = :pk AND begins_with(sk, :prefix)',
            ExpressionAttributeValues={':pk': GLOBAL_PREFERENCES_PK, ':prefix': 'PREF#'}
        )
        preferences.extend(item['statement'] for item in resp.get('Items', []))
    except Exception as e:
        logger.warning("Failed to load global preferences: %s", e)

    if account_id:
        try:
            resp = table.query(
                KeyConditionExpression='pk = :pk AND begins_with(sk, :prefix)',
                ExpressionAttributeValues={':pk': f'ACCOUNT#{account_id}', ':prefix': 'PREF#'}
            )
            preferences.extend(item['statement'] for item in resp.get('Items', []))
        except Exception as e:
            logger.warning("Failed to load preferences for %s: %s", account_id, e)

    if not preferences:
        return "(no preferences defined)"
    return "\n".join(f"- {p}" for p in preferences)


def _format_list(items: list) -> str:
    """Format a list as bullet points, or '(none)' if empty."""
    return "\n".join(f"- {item}" for item in items) if items else "(none)"


def _format_grouped_inventory(account_data: dict) -> str:
    """Format account inventory as a compact region → service → resource_types hierarchy.

    Uses the 'inventory' field (grouped format) if available, otherwise falls
    back to the flat 'services' list.

    Output format:
        eu-west-1:
          ec2: dhcp-options, network-acl, subnet, vpc
          athena: workgroup
        us-east-1:
          s3: bucket
          lambda: function
    """
    inventory = account_data.get("inventory")
    if not inventory:
        # Fallback: flat services list (old format without region grouping)
        services = account_data.get("services", [])
        if not services:
            return "(none)"
        return "\n".join(f"- {s}" for s in services)

    lines = []
    for region, services in inventory.items():
        lines.append(f"{region}:")
        for service, resource_types in services.items():
            if resource_types:
                # Strip service prefix from resource types for brevity
                # e.g. "ec2:dhcp-options" → "dhcp-options", "athena:workgroup" → "workgroup"
                short_types = []
                for rt in resource_types:
                    if ":" in rt:
                        short_types.append(rt.split(":", 1)[1])
                    else:
                        short_types.append(rt)
                lines.append(f"  {service}: {', '.join(short_types)}")
            else:
                lines.append(f"  {service}")
    return "\n".join(lines) if lines else "(none)"


def _create_memory_session_manager(agent_name: str, account_id: str = ''):
    """Create AgentCore memory session manager, or None if disabled."""
    if not ENABLE_MEMORY or not MEMORY_ID:
        return None

    try:
        from bedrock_agentcore.memory.integrations.strands.config import (
            AgentCoreMemoryConfig, RetrievalConfig,
        )
        from bedrock_agentcore.memory.integrations.strands.session_manager import (
            AgentCoreMemorySessionManager,
        )

        memory_id = MEMORY_ID.rsplit('/', 1)[-1] if MEMORY_ID.startswith('arn:') else MEMORY_ID
        actor_id = f'awana-{account_id}' if account_id else 'awana'
        session_id = f"eval-{agent_name}-{uuid.uuid4().hex[:12]}"

        config = AgentCoreMemoryConfig(
            memory_id=memory_id,
            session_id=session_id,
            actor_id=actor_id,
            retrieval_config={
                '/preferences/{actorId}': RetrievalConfig(top_k=10, relevance_score=0.7),
            },
        )
        return AgentCoreMemorySessionManager(config, region_name=region)
    except Exception as e:
        logger.error("Failed to create memory session manager: %s", e)
        return None


# ─── One-shot agent invocation (shared pattern) ───


def _extract_token_usage(result) -> dict:
    """Pull token usage out of a Strands agent result.

    Returns an empty dict if metrics are unavailable. Keys mirror the
    Strands ``accumulated_usage`` shape but are renamed to snake_case for
    consumption by downstream Python code.
    """
    try:
        metrics = getattr(result, 'metrics', None)
        if metrics is None:
            return {}
        usage = getattr(metrics, 'accumulated_usage', {}) or {}
        return {
            'input_tokens': usage.get('inputTokens', 0),
            'output_tokens': usage.get('outputTokens', 0),
            'total_tokens': usage.get('totalTokens', 0),
            'cache_read_tokens': usage.get('cacheReadInputTokens', 0),
            'cache_write_tokens': usage.get('cacheWriteInputTokens', 0),
        }
    except Exception:
        return {}


def _format_decision_footer(agent_name: str, tokens: dict) -> str:
    """Build a one-line attribution footer to append to a reasoning string.

    Includes the deciding agent and its token usage so the reasoning that
    is persisted to DynamoDB and surfaced to users records both *who*
    decided and *how much* the decision cost.
    """
    if not tokens:
        return f"\n\n— Decided by agent '{agent_name}'."
    return (
        f"\n\n— Decided by agent '{agent_name}' "
        f"(tokens: input={tokens.get('input_tokens', 0)}, "
        f"output={tokens.get('output_tokens', 0)}, "
        f"total={tokens.get('total_tokens', 0)})."
    )


def _log_token_metrics(agent_name: str, result, mode: str = ""):
    """Log token usage metrics from an agent invocation as structured JSON.

    Emits a single JSON log line with input/output/total token counts,
    queryable in CloudWatch Logs Insights.
    """
    try:
        usage = _extract_token_usage(result)
        if not usage:
            return

        metrics = getattr(result, 'metrics', None)
        cycle_durations = getattr(metrics, 'cycle_durations', []) or []
        total_duration = sum(cycle_durations)

        logger.info(
            "TOKEN_METRICS %s",
            json.dumps({
                "metric_type": "token_usage",
                "agent_name": agent_name,
                "mode": mode,
                "input_tokens": usage.get('input_tokens', 0),
                "output_tokens": usage.get('output_tokens', 0),
                "total_tokens": usage.get('total_tokens', 0),
                "cache_read_tokens": usage.get('cache_read_tokens', 0),
                "cache_write_tokens": usage.get('cache_write_tokens', 0),
                "duration_seconds": round(total_duration, 3),
                "cycles": len(cycle_durations),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }),
        )
    except Exception as e:
        logger.debug("Failed to log token metrics for %s: %s", agent_name, e)


def invoke_one_shot(config_id: str, announcement_text: str, *,
                    structured_model, context_data: Optional[Dict[str, str]] = None,
                    system_prompt: str = "") -> Any:
    """Load config from SSM, inject context, invoke agent, return structured output.

    This is the shared pattern for pre-filter, service-routing, and any
    single-agent invocation that doesn't need the multi-agent graph.
    """
    structured, _ = invoke_one_shot_with_tokens(
        config_id, announcement_text,
        structured_model=structured_model,
        context_data=context_data,
        system_prompt=system_prompt,
    )
    return structured


def invoke_one_shot_with_tokens(config_id: str, announcement_text: str, *,
                                structured_model, context_data: Optional[Dict[str, str]] = None,
                                system_prompt: str = "") -> tuple[Any, dict]:
    """Same as ``invoke_one_shot`` but also returns the token usage dict.

    Useful when callers want to surface token cost in the response (for
    example, to attribute a classify decision to a specific agent).
    """
    config = load_agent_config(config_id)
    prompt = config['prompt']

    if context_data:
        for key, value in context_data.items():
            prompt = prompt.replace('{' + key + '}', value)

    if system_prompt:
        prompt = f"{system_prompt}\n---\n{prompt}"

    model = BedrockModel(model_id=config['model_id'])
    agent = Agent(model=model, system_prompt=prompt, structured_output_model=structured_model)

    result = agent(announcement_text)
    _log_token_metrics(config_id, result, mode="one-shot")
    return result.structured_output, _extract_token_usage(result)


# ─── Sequential agent pipeline (replaces GraphBuilder) ───


def run_pipeline(agent_configs: list[dict], announcement_text: str, *,
                 system_prompt: str = "", account_id: str = "") -> dict:
    """Run agents sequentially until one gives a conclusive answer.

    Each entry in agent_configs: {"name": str, "config_id": str, "context_data": dict}

    Stops when an agent returns "relevant" or "not relevant".
    Continues to the next agent on "not my scope".
    The last agent MUST give a conclusive answer.

    Returns ``{"result": str, "reasoning": str, "execution_order": [str],
    "decided_by": str, "tokens": {...}}``. The ``reasoning`` string is
    suffixed with an attribution footer naming the deciding agent and its
    token usage.
    """
    execution_order: list[str] = []

    for i, agent_cfg in enumerate(agent_configs):
        name = agent_cfg['name']
        config_id = agent_cfg['config_id']
        context_data = agent_cfg.get('context_data')
        is_last = (i == len(agent_configs) - 1)

        logger.info("Pipeline step %d/%d: agent '%s'", i + 1, len(agent_configs), name)

        try:
            config = load_agent_config(config_id)
            prompt = config['prompt']

            if context_data:
                for key, value in context_data.items():
                    prompt = prompt.replace('{' + key + '}', value)

            combined_prompt = f"{system_prompt}\n---\n{prompt}" if system_prompt else prompt

            kwargs: dict[str, Any] = dict(
                model=BedrockModel(model_id=config['model_id']),
                system_prompt=combined_prompt,
                structured_output_model=AnnouncementRelevance,
            )

            session_manager = _create_memory_session_manager(name, account_id=account_id)
            if session_manager:
                kwargs['session_manager'] = session_manager

            agent = Agent(**kwargs)
            result = agent(announcement_text)
            structured = result.structured_output
            execution_order.append(name)

            tokens = _extract_token_usage(result)
            _log_token_metrics(name, result, mode="pipeline")

            result_text = structured.result.lower().strip()
            logger.info("Agent '%s': result='%s', reasoning='%s'", name, result_text, structured.reasoning[:100])

            if result_text in ('relevant', 'not relevant') or is_last:
                reasoning_with_attribution = (
                    structured.reasoning + _format_decision_footer(name, tokens)
                )
                return {
                    "result": structured.result,
                    "reasoning": reasoning_with_attribution,
                    "execution_order": execution_order,
                    "decided_by": name,
                    "tokens": tokens,
                }
            # "not my scope" → continue to next agent

        except Exception as e:
            logger.error("Agent '%s' failed: %s", name, e, exc_info=True)
            execution_order.append(name)
            if is_last:
                reasoning = f"Final agent failed: {e}" + _format_decision_footer(name, {})
                return {
                    "result": "not relevant",
                    "reasoning": reasoning,
                    "execution_order": execution_order,
                    "decided_by": name,
                    "tokens": {},
                }
            # Non-final agent failure → skip to next

    return {
        "result": "not relevant",
        "reasoning": "No agent produced output",
        "execution_order": execution_order,
        "decided_by": "",
        "tokens": {},
    }


# ─── Mode handlers ───


def handle_pre_filter(parsed: InvocationRequest) -> dict:
    """LLM-based pre-filter: extract services/engines/platforms, match vs inventory."""
    context = load_consolidated_context()
    if context is None:
        return {"pass": True, "reason": "Context file unavailable, passing through"}

    prompt_text = parsed.input.get('prompt', '')
    announcement = parse_announcement(prompt_text)
    if announcement is None:
        return {"pass": True, "reason": "Could not parse announcement, passing through"}

    org_wide = context.get('org_wide', {})
    context_data = {
        'org_services': _format_list(org_wide.get('services', [])),
        'org_cache_engines': _format_list(org_wide.get('cache_engines', [])),
        'org_database_engines': _format_list(org_wide.get('database_engines', [])),
        'org_ec2_platforms': _format_list(org_wide.get('platforms', [])),
        'org_instance_types': _format_list(org_wide.get('instance_types', [])),
        'org_regions': _format_list(org_wide.get('regions', [])),
    }

    announcement_text = f"Title: {announcement['title']}\nDescription: {announcement['description']}"

    try:
        structured = invoke_one_shot('pre-filter', announcement_text,
                                     structured_model=PreFilterResult, context_data=context_data)
        return {
            "pass": structured.passes,
            "reason": structured.reason,
            "extracted": {
                "services": structured.services,
                "database_engines": structured.database_engines,
                "cache_engines": structured.cache_engines,
                "ec2_platforms": structured.ec2_platforms,
                "regions": structured.regions,
            },
        }
    except Exception as e:
        logger.error("Pre-filter failed: %s — passing through", e, exc_info=True)
        return {"pass": True, "reason": f"Pre-filter error, passing through: {e}"}


def handle_service_routing(parsed: InvocationRequest) -> dict:
    """LLM-based service routing: identify primary service, classify single/multi."""
    fallback = {"route": "multi_service", "matched_service": "", "services": []}

    context = load_consolidated_context()
    if context is None:
        return fallback

    raw_prompt = parsed.input.get("prompt", "")
    announcement = parse_announcement(raw_prompt)
    if announcement is None:
        return fallback

    org_services = context.get("org_wide", {}).get("services", [])
    context_data = {'service_list': "\n".join(org_services)}
    announcement_text = f"{announcement['title']}\n{announcement['description']}"

    try:
        structured = invoke_one_shot('service-router', announcement_text,
                                     structured_model=ServiceRoutingResult, context_data=context_data)
        return {
            "route": structured.route,
            "matched_service": structured.matched_service,
            "services": structured.services,
        }
    except Exception as e:
        logger.error("Service routing failed: %s", e, exc_info=True)
        return fallback


def handle_classify(parsed: InvocationRequest) -> dict:
    """Combined account-agnostic classification: general → pre-filter → service-routing.

    Runs all account-agnostic checks in a single invocation, short-circuiting
    when a conclusive result is reached:

    1. General Category Filter — catches universally relevant announcements
       (new services, billing, IAM). If "relevant" → done for all accounts.
    2. Pre-Filter — extracts services/engines/platforms and matches vs inventory.
       If nothing matches → "not relevant" for all accounts.
    3. Service Router — identifies primary service and classifies as single/multi.

    Returns a unified response:
    {
        "decision": "relevant_all" | "not_relevant_all" | "single_service" | "per_group",
        "result": str,           # "relevant" or "not relevant" (for _all decisions)
        "reasoning": str,
        "matched_service": str,  # only for single_service
        "services": [str],       # only for single_service / per_group
    }
    """
    prompt_text = parsed.input.get('prompt', '')
    announcement = parse_announcement(prompt_text)
    if announcement is None:
        return {"decision": "per_group", "result": "", "reasoning": "Could not parse announcement",
                "matched_service": "", "services": []}

    announcement_text = f"Title: {announcement['title']}\nDescription: {announcement['description']}"
    system_prompt = load_system_prompt()

    # ── Step 1: Account-Agnostic Classifier (preferences + general categories) ──
    logger.info("Classify step 1: Account-Agnostic Classifier")
    context_data = {'preferences': _fetch_preferences()}

    try:
        classifier_result, classifier_tokens = invoke_one_shot_with_tokens(
            'account-agnostic-classifier', prompt_text,
            structured_model=AnnouncementRelevance,
            context_data=context_data,
            system_prompt=system_prompt,
        )
        result_text = classifier_result.result.lower().strip()
        logger.info("Account-agnostic classifier: result='%s'", result_text)

        if result_text == "relevant":
            logger.info("Classify: account-agnostic classifier → relevant for all")
            reasoning = classifier_result.reasoning + _format_decision_footer('account-agnostic-classifier', classifier_tokens)
            return {"decision": "relevant_all", "result": classifier_result.result,
                    "reasoning": reasoning, "matched_service": "", "services": []}
        if result_text == "not relevant":
            logger.info("Classify: account-agnostic classifier → not relevant for all")
            reasoning = classifier_result.reasoning + _format_decision_footer('account-agnostic-classifier', classifier_tokens)
            return {"decision": "not_relevant_all", "result": classifier_result.result,
                    "reasoning": reasoning, "matched_service": "", "services": []}
    except Exception as e:
        logger.error("Classify: account-agnostic classifier failed, continuing: %s", e)
        # On failure, fall through to pre-filter (safe default)

    # ── Step 2: Pre-Filter ──
    logger.info("Classify step 2: Pre-Filter")
    context = load_consolidated_context()
    if context is None:
        # Can't pre-filter without context — fall through to per-group
        return {"decision": "per_group", "result": "", "reasoning": "Context unavailable",
                "matched_service": "", "services": []}

    org_wide = context.get('org_wide', {})
    pf_context = {
        'org_services': _format_list(org_wide.get('services', [])),
        'org_cache_engines': _format_list(org_wide.get('cache_engines', [])),
        'org_database_engines': _format_list(org_wide.get('database_engines', [])),
        'org_ec2_platforms': _format_list(org_wide.get('platforms', [])),
        'org_instance_types': _format_list(org_wide.get('instance_types', [])),
        'org_regions': _format_list(org_wide.get('regions', [])),
    }

    try:
        pf_result, pf_tokens = invoke_one_shot_with_tokens(
            'pre-filter', announcement_text,
            structured_model=PreFilterResult, context_data=pf_context,
        )
        if not pf_result.passes:
            logger.info("Classify: pre-filter rejected — %s", pf_result.reason)
            reasoning = pf_result.reason + _format_decision_footer('pre-filter', pf_tokens)
            return {"decision": "not_relevant_all", "result": "not relevant",
                    "reasoning": reasoning, "matched_service": "", "services": []}
    except Exception as e:
        logger.error("Classify: pre-filter failed, continuing: %s", e)
        # On failure, pass through to service routing (safe default)

    # ── Step 3: Service Router ──
    logger.info("Classify step 3: Service Router")
    org_services = org_wide.get("services", [])
    sr_context = {'service_list': "\n".join(org_services)}

    try:
        sr_result, sr_tokens = invoke_one_shot_with_tokens(
            'service-router', announcement_text,
            structured_model=ServiceRoutingResult, context_data=sr_context,
        )
        if sr_result.route == "single_service" and sr_result.matched_service:
            logger.info("Classify: single-service match → %s", sr_result.matched_service)
            reasoning = (
                f"Single service: {sr_result.matched_service}"
                + _format_decision_footer('service-router', sr_tokens)
            )
            return {"decision": "single_service", "result": "",
                    "reasoning": reasoning,
                    "matched_service": sr_result.matched_service, "services": sr_result.services}
        else:
            logger.info("Classify: multi-service → per-group evaluation")
            reasoning = (
                "Multi-service announcement, needs per-account evaluation"
                + _format_decision_footer('service-router', sr_tokens)
            )
            return {"decision": "per_group", "result": "",
                    "reasoning": reasoning,
                    "matched_service": "", "services": sr_result.services}
    except Exception as e:
        logger.error("Classify: service routing failed, defaulting to per_group: %s", e)
        return {"decision": "per_group", "result": "", "reasoning": f"Service routing failed: {e}",
                "matched_service": "", "services": []}


def handle_evaluate(parsed: InvocationRequest) -> dict:
    """Evaluate announcement relevance for a specific account group or generically.

    When account_group is provided, uses the single per-account-evaluator agent
    that combines preference matching, EC2/RDS specialist logic, and service
    usage correlation into one LLM call (replaces the former 4-agent pipeline).

    When no account_group is provided, falls back to the account-agnostic
    preferences → general-category pipeline.
    """
    user_message = parsed.input.get("prompt", "")
    if not user_message:
        raise HTTPException(status_code=400, detail="No prompt in input")

    account_group = parsed.input.get("account_group")
    system_prompt = load_system_prompt()

    if account_group:
        # Per-group evaluation: single merged agent
        account_ids = account_group.get("account_ids", [])
        account_id = account_ids[0] if account_ids else ""

        context = load_consolidated_context()
        if context and account_id:
            # Find the account's group data
            group = next((g for g in context.get('account_groups', [])
                         if account_id in g.get('account_ids', [])), None)
            account_data = context.get('accounts', {}).get(account_id, {})
            context_data = {
                'preferences': _fetch_preferences(account_id),
                'ec2_instance_types': _format_list(group.get('instance_types', []) if group else []),
                'ec2_platforms': _format_list(group.get('platforms', []) if group else []),
                'database_engines': _format_list(group.get('database_engines', []) if group else []),
                'cache_engines': _format_list(group.get('cache_engines', []) if group else []),
                'resource_inventory': _format_grouped_inventory(account_data) if account_data else "(none)",
            }
        else:
            context_data = {
                'preferences': _fetch_preferences(account_id),
                'ec2_instance_types': "(none)",
                'ec2_platforms': "(none)",
                'database_engines': "(none)",
                'cache_engines': "(none)",
                'resource_inventory': "(none)",
            }

        try:
            structured, tokens = invoke_one_shot_with_tokens(
                'per-account-evaluator', user_message,
                structured_model=AnnouncementRelevance,
                context_data=context_data,
                system_prompt=system_prompt,
            )
            reasoning = structured.reasoning + _format_decision_footer('per-account-evaluator', tokens)
            return {
                "result": structured.result,
                "reasoning": reasoning,
                "execution_order": ["per-account-evaluator"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            logger.error("Per-account evaluation failed: %s", e, exc_info=True)
            return {
                "result": "not relevant",
                "reasoning": f"Evaluation failed: {e}",
                "execution_order": ["per-account-evaluator"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    else:
        # Account-agnostic evaluation: merged preferences + general categories
        context_data = {'preferences': _fetch_preferences()}

        try:
            structured, tokens = invoke_one_shot_with_tokens(
                'account-agnostic-classifier', user_message,
                structured_model=AnnouncementRelevance,
                context_data=context_data,
                system_prompt=system_prompt,
            )
            reasoning = structured.reasoning + _format_decision_footer('account-agnostic-classifier', tokens)
            return {
                "result": structured.result,
                "reasoning": reasoning,
                "execution_order": ["account-agnostic-classifier"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            logger.error("Account-agnostic evaluation failed: %s", e, exc_info=True)
            return {
                "result": "not relevant",
                "reasoning": f"Evaluation failed: {e}",
                "execution_order": ["account-agnostic-classifier"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }


# ─── FastAPI app ───


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=== Awana Agent Server starting up ===")
    logger.info("AWS_REGION=%s, ENABLE_MEMORY=%s", region, ENABLE_MEMORY)
    # Pre-warm caches
    load_system_prompt()
    load_consolidated_context()
    logger.info("Startup complete")
    yield
    logger.info("=== Awana Agent Server shutting down ===")


app = FastAPI(title="Awana Agent Server", version="2.0.0", lifespan=lifespan)


@app.post("/invocations", response_model=InvocationResponse)
async def invoke_agent(request: Request):
    try:
        body = await request.json()
        parsed = InvocationRequest(**body)
        mode = parsed.input.get("mode", "evaluate")
        logger.info("Invocation mode='%s'", mode)

        if mode == "classify":
            return InvocationResponse(output=handle_classify(parsed))
        elif mode == "pre-filter":
            return InvocationResponse(output=handle_pre_filter(parsed))
        elif mode == "service-routing":
            return InvocationResponse(output=handle_service_routing(parsed))
        else:
            return InvocationResponse(output=handle_evaluate(parsed))

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Invocation failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.get("/ping")
async def ping():
    return {"status": "healthy"}


# ─── Server entry point ───
# AgentCore direct-code-deployment expects the entry point file to start
# an HTTP server on port 8080. Without this block, `python agent.py` runs
# the module-level code, creates the FastAPI app, and exits — leaving no
# server listening, which causes the runtime to time out after 30s with
# "Runtime initialization time exceeded".
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
