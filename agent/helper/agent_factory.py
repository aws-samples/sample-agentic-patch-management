"""Common agent factory to eliminate duplication"""

import os
import re
import sys
import logging
from botocore.config import Config as BotocoreConfig
from strands import Agent
from strands.models import BedrockModel
from bedrock_agentcore.memory.integrations.strands.config import (
    AgentCoreMemoryConfig,
    RetrievalConfig,
)
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from helper.memory_hooks import ReadOnlyMemoryHook
from helper.template_loader import get_templates_for_agent

logger = logging.getLogger(__name__)

# Shared retry config for memory API calls
_RETRY_CONFIG = BotocoreConfig(
    retries={"max_attempts": 10, "mode": "adaptive"}
)


def _build_actor_id(operator_id: str | None) -> str:
    """Build operator-scoped actor_id for memory isolation (GAP-2).

    actor_id must match: [a-zA-Z0-9][a-zA-Z0-9-_/]*
    Strip any chars not in that set (e.g. '@' from 'operator@session').
    """
    import re
    base = operator_id or "anonymous"
    # Replace invalid chars with '-', collapse multiple dashes
    sanitized = re.sub(r'[^a-zA-Z0-9\-_/]', '-', base)
    sanitized = re.sub(r'-{2,}', '-', sanitized).strip('-')
    return f"patch-automation/{sanitized}" if sanitized else "patch-automation/anonymous"


def create_agent(name: str, system_prompt: str, tools: list, context, max_tokens: int = 6000) -> Agent:
    """
    Create agent with AgentCore memory integration.

    Uses the official AgentCoreMemorySessionManager (aligned with the Agentic AI
    Accelerator reference architecture) for the supervisor. Specialists get a
    lightweight read-only hook that loads the supervisor's conversation context
    without writing to memory.

    Args:
        name: Agent name (e.g., "supervisor", "vulnerability_analyst")
        system_prompt: Agent's system prompt
        tools: List of tools available to agent
        context: AgentCore context with session_id, memory_id, operator_id,
                 and optional memory_read_only
        max_tokens: Maximum tokens for response (default 6000, 12000 for patch_manager)

    Returns:
        Configured Agent instance
    """
    memory_id = getattr(context, 'memory_id', None)
    session_id = getattr(context, 'session_id', None)
    operator_id = getattr(context, 'operator_id', None)
    memory_read_only = getattr(context, 'memory_read_only', False)

    if not memory_id:
        logger.warning(f"No memory_id in context for {name}, memory will not persist")

    actor_id = _build_actor_id(operator_id)

    # --- Memory integration ---
    session_manager = None
    hooks = []

    if memory_id and not memory_read_only:
        # Supervisor: use the official AgentCoreMemorySessionManager.
        # This handles STM persistence, session rehydration, and LTM retrieval
        # on every user message (via retrieve_customer_context hook).
        retrieval_config = _build_retrieval_config(actor_id)

        config = AgentCoreMemoryConfig(
            memory_id=memory_id,
            session_id=session_id,
            actor_id=actor_id,
            retrieval_config=retrieval_config,
        )
        logger.info(f"[MEMORY] Creating session manager: actor={actor_id} session={session_id} retrieval={retrieval_config}")
        try:
            session_manager = AgentCoreMemorySessionManager(
                boto_client_config=_RETRY_CONFIG,
                agentcore_memory_config=config,
            )
            logger.info(f"[MEMORY] Session manager created OK for {name}")
        except Exception as e:
            logger.error(f"[MEMORY] ERROR creating session manager: {e}")
            session_manager = None

    elif memory_id and memory_read_only:
        # Specialists: read-only hook loads supervisor's conversation context
        hooks.append(ReadOnlyMemoryHook(
            memory_id=memory_id,
            actor_id=actor_id,
            session_id=session_id,
        ))
        logger.info(f"[MEMORY] Read-only hook for {name} (actor={actor_id})")

    # Inject response templates for this agent
    full_prompt = system_prompt + get_templates_for_agent(name)

    # Guardrail config (opt-in via env vars — see setup_guardrail.py)
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
        }
        logger.info(f"Guardrail enabled for {name}: {guardrail_id} v{guardrail_version}")

    # Create and return agent
    return Agent(
        name=name,
        model=BedrockModel(
            model_id=os.environ.get('BEDROCK_MODEL_ID', 'us.anthropic.claude-sonnet-4-6-v1:0'),
            region_name=os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')),
            temperature=0.1,
            max_tokens=max_tokens,
            **guardrail_kwargs
        ),
        system_prompt=full_prompt,
        tools=tools,
        hooks=hooks,
        session_manager=session_manager,
    )


def _build_retrieval_config(actor_id: str) -> dict[str, RetrievalConfig] | None:
    """Build LTM retrieval config for cross-session context (GAP-3).

    The session manager resolves namespace templates using:
      namespace.format(actorId=actor_id, sessionId=session_id, memoryStrategyId=strategy_id)

    We use the actor-level prefix namespace so the API does a prefix match across
    all strategies and sessions for this operator. The {sessionId} is left as a
    literal placeholder — the API treats it as a prefix match up to that point.

    Namespace pattern: /actor/{actorId}/strategy/{memoryStrategyId}/{sessionId}
    For cross-session retrieval we omit {sessionId} to search across all sessions.
    """
    # Namespace resolves to /actor/<actor_id>/strategy/<strategy_id>
    # This is a prefix that covers all sessions for this operator+strategy.
    namespace_template = "/actor/{actorId}/strategy/{memoryStrategyId}"

    return {
        namespace_template: RetrievalConfig(
            top_k=5,
            relevance_score=0.3,
        )
    }
