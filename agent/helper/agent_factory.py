"""Agent factory — creates the agent with memory, steering, and guardrails."""

import logging
import os
import re
from typing import Optional

from strands import Agent
from strands.models import BedrockModel, CacheConfig
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from helper.steering import (
    PatchWorkflowSteering,
    ComplianceOutputSteering,
)

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = 'us.anthropic.claude-sonnet-5'

# Models that require adaptive thinking (always-on, cannot be disabled).
# All Sonnet 5+ variants require this — intentionally broad matching.
_ADAPTIVE_THINKING_MODELS = ('claude-sonnet-5', 'claude-sonnet-6')
_EFFORTS = ('low', 'medium', 'high')
DEFAULT_EFFORT = 'medium'  # Release B: same routing as the default, ~30% less output on the eval


def model_request_fields(model_id: str):
    """Extra Bedrock request fields for a model: adaptive thinking and, if set, its effort.

    Shared by the agent and the tool-selection eval so both send the same request.
    THINKING_EFFORT (low, medium or high) overrides DEFAULT_EFFORT; other values are ignored.
    """
    if not any(base in model_id for base in _ADAPTIVE_THINKING_MODELS):
        return None
    fields = {"thinking": {"type": "adaptive"}}
    effort = (os.environ.get('THINKING_EFFORT') or '').lower()
    fields["output_config"] = {"effort": effort if effort in _EFFORTS else DEFAULT_EFFORT}
    return fields


def _build_actor_id(operator_id: str | None) -> str:
    """Build operator-scoped actor_id for memory isolation."""
    base = operator_id or "anonymous"
    sanitized = re.sub(r'[^a-zA-Z0-9\-_/]', '-', base)
    sanitized = re.sub(r'-{2,}', '-', sanitized).strip('-')
    return f"patch-automation/{sanitized}" if sanitized else "patch-automation/anonymous"


def create_agent(name: str, system_prompt: str, tools: list, context, max_tokens: int = 6000,
                 hooks: Optional[list] = None) -> Agent:
    """Create agent with AgentCore memory integration."""
    memory_id = getattr(context, 'memory_id', None)
    session_id = getattr(context, 'session_id', None)
    operator_id = getattr(context, 'operator_id', None)

    if not memory_id:
        logger.warning(f"No memory_id in context for {name}, memory will not persist")

    actor_id = _build_actor_id(operator_id)
    aws_region = os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))

    session_manager = None
    if memory_id:
        # LTM retrieval disabled — injects stale command IDs into new sessions
        config = AgentCoreMemoryConfig(
            memory_id=memory_id,
            session_id=session_id,
            actor_id=actor_id,
        )
        session_manager = AgentCoreMemorySessionManager(
            agentcore_memory_config=config,
            region_name=aws_region,
        )
        logger.info(f"[MEMORY] Session manager for {name} (actor={actor_id}, session={session_id})")

    guardrail_kwargs = {}
    guardrail_id = os.environ.get('GUARDRAIL_ID')
    guardrail_version = os.environ.get('GUARDRAIL_VERSION')
    if guardrail_id and guardrail_version:
        guardrail_kwargs = {
            'guardrail_id': guardrail_id,
            'guardrail_version': guardrail_version,
            'guardrail_trace': 'enabled',
            'guardrail_stream_processing_mode': 'async',
            'guardrail_redact_input': True,
            'guardrail_redact_output': False,
            # Screen the operator's new message, not the whole replayed conversation,
            # which was already screened on earlier turns and is billed per call.
            'guardrail_latest_message': True,
        }
        logger.info(f"Guardrail enabled for {name}: {guardrail_id} v{guardrail_version}")

    # Steering: deterministic workflow enforcement (no extra LLM calls)
    plugins = [PatchWorkflowSteering(), ComplianceOutputSteering()]
    for plugin in plugins:
        logger.info(f"[STEERING] {plugin.__class__.__name__} enabled for {name}")

    model_id = os.environ.get('BEDROCK_MODEL_ID', _DEFAULT_MODEL)

    # Enable thinking for models that support it (Sonnet 5+).
    # Adaptive thinking lets the model reason before tool selection —
    # improves plan quality and reduces tool selection errors.
    model_kwargs = {
        'model_id': model_id,
        'region_name': aws_region,
        'max_tokens': max_tokens,
        'cache_config': CacheConfig(strategy="auto"),
        **guardrail_kwargs,
    }
    request_fields = model_request_fields(model_id)
    if request_fields:
        model_kwargs['additional_request_fields'] = request_fields
        logger.info(f"[MODEL] {model_id} request fields {request_fields}")
    else:
        model_kwargs['temperature'] = 0.1

    return Agent(
        name=name,
        model=BedrockModel(**model_kwargs),
        system_prompt=system_prompt,
        tools=list(tools),
        plugins=plugins,
        session_manager=session_manager,
        hooks=hooks or [],
    )


