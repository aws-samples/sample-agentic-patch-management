"""Common agent factory to eliminate duplication"""

import globals
from strands import Agent
from strands.models import BedrockModel
from bedrock_agentcore.memory import MemoryClient
from memory_hooks import AgentCoreMemoryHook


def create_agent(name: str, system_prompt: str, tools: list, context) -> Agent:
    """
    Create agent with AgentCore memory integration.
    
    Args:
        name: Agent name (e.g., "supervisor", "vulnerability_analyst")
        system_prompt: Agent's system prompt
        tools: List of tools available to agent
        context: AgentCore context with session_id
    
    Returns:
        Configured Agent instance
    """
    # Initialize memory
    memory_client = MemoryClient()
    memory_id = getattr(context, 'memory_id', None) or "intelligent_patch_automation_praveen_mem-lMRdfD5RJP"
    
    # Set global context for cross-agent memory sharing
    globals.MEMORY_ID = memory_id
    globals.SESSION_ID = context.session_id
    
    # Create memory hook
    memory_hook = AgentCoreMemoryHook(
        memory_client=memory_client,
        memory_id=memory_id,
        actor_id=f"{name}_agent",
        session_id=context.session_id
    )
    
    # Create and return agent
    return Agent(
        name=name,
        model=BedrockModel(
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            region_name='us-east-1',
            temperature=0.1,
            max_tokens=4000
        ),
        system_prompt=system_prompt,
        tools=tools,
        hooks=[memory_hook]
    )
