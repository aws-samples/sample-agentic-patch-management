"""Common agent factory to eliminate duplication"""

import os
from helper import globals
import logging
from strands import Agent
from strands.models import BedrockModel
from bedrock_agentcore.memory import MemoryClient
from helper.memory_hooks import AgentCoreMemoryHook

logger = logging.getLogger(__name__)


def create_agent(name: str, system_prompt: str, tools: list, context, max_tokens: int = 6000) -> Agent:
    """
    Create agent with AgentCore memory integration.
    
    Args:
        name: Agent name (e.g., "supervisor", "vulnerability_analyst")
        system_prompt: Agent's system prompt
        tools: List of tools available to agent
        context: AgentCore context with session_id
        max_tokens: Maximum tokens for response (default 6000, 12000 for patch_manager)
    
    Returns:
        Configured Agent instance
    """
    # Initialize memory
    memory_client = MemoryClient()
    memory_id = getattr(context, 'memory_id', None)
    
    if not memory_id:
        logger.warning(f"No memory_id in context for {name}, memory will not persist")
    
    # Set global context for cross-agent memory sharing
    globals.MEMORY_ID = memory_id
    globals.SESSION_ID = context.session_id
    
    # Create memory hook only if memory_id available
    hooks = []
    if memory_id:
        memory_hook = AgentCoreMemoryHook(
            memory_client=memory_client,
            memory_id=memory_id,
            actor_id=f"{name}_agent",
            session_id=context.session_id
        )
        hooks.append(memory_hook)
    
    # Create and return agent
    return Agent(
        name=name,
        model=BedrockModel(
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            region_name=os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')),
            temperature=0.1,
            max_tokens=max_tokens
        ),
        system_prompt=system_prompt,
        tools=tools,
        hooks=hooks
    )
