"""Common agent factory to eliminate duplication"""

import os
import logging
from strands import Agent
from strands.models import BedrockModel
from bedrock_agentcore.memory import MemoryClient
from helper.memory_hooks import AgentCoreMemoryHook
from helper.template_loader import get_templates_for_agent

logger = logging.getLogger(__name__)


def create_agent(name: str, system_prompt: str, tools: list, context, max_tokens: int = 6000) -> Agent:
    """
    Create agent with AgentCore memory integration.
    
    Args:
        name: Agent name (e.g., "supervisor", "vulnerability_analyst")
        system_prompt: Agent's system prompt
        tools: List of tools available to agent
        context: AgentCore context with session_id, memory_id, operator_id, and optional memory_read_only
        max_tokens: Maximum tokens for response (default 6000, 12000 for patch_manager)
    
    Returns:
        Configured Agent instance
    """
    # Initialize memory
    memory_client = MemoryClient()
    memory_id = getattr(context, 'memory_id', None)
    session_id = getattr(context, 'session_id', None)
    operator_id = getattr(context, 'operator_id', None)
    memory_read_only = getattr(context, 'memory_read_only', False)
    
    if not memory_id:
        logger.warning(f"No memory_id in context for {name}, memory will not persist")
    
    # Create memory hook only if memory_id available
    hooks = []
    if memory_id:
        # actor_id scoped per operator for memory isolation (GAP-2).
        # Long-term memory namespaces use {actorId} as a template variable,
        # so operator-scoped actor_id gives us per-operator memory isolation for free.
        # Specialists use the SAME actor_id (read-only) so they can see the
        # operator's conversation context without polluting it.
        actor_id = f"patch-automation/{operator_id}" if operator_id else "patch-automation/anonymous"
        
        memory_hook = AgentCoreMemoryHook(
            memory_client=memory_client,
            memory_id=memory_id,
            actor_id=actor_id,
            session_id=session_id,
            read_only=memory_read_only
        )
        hooks.append(memory_hook)
    
    # Inject response templates for this agent
    full_prompt = system_prompt + get_templates_for_agent(name)

    # Create and return agent
    return Agent(
        name=name,
        model=BedrockModel(
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            region_name=os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')),
            temperature=0.1,
            max_tokens=max_tokens
        ),
        system_prompt=full_prompt,
        tools=tools,
        hooks=hooks
    )
