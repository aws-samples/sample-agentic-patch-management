"""
Strands Memory Hooks for AgentCore Integration - Patch Automation
"""

import logging
from typing import List, Tuple
from strands.hooks import AgentInitializedEvent, HookProvider, HookRegistry, MessageAddedEvent
from bedrock_agentcore.memory import MemoryClient

logger = logging.getLogger(__name__)

class AgentCoreMemoryHook(HookProvider):
    """
    Memory hook provider that integrates Strands agents with AgentCore memory.
    
    Enables persistent conversations across vulnerability response workflows.
    """
    
    def __init__(self, memory_client: MemoryClient, memory_id: str, actor_id: str, session_id: str):
        """
        Initialize the memory hook provider.
        
        Args:
            memory_client: AgentCore memory client instance
            memory_id: AgentCore memory resource ID  
            actor_id: Unique identifier for this agent (supervisor_agent, etc.)
            session_id: Session identifier for conversation isolation
        """
        self.memory_client = memory_client
        self.memory_id = memory_id
        self.actor_id = actor_id
        self.session_id = session_id
    
    def on_agent_initialized(self, event: AgentInitializedEvent):
        """Load recent conversation history when agent starts."""
        try:
            logger.info(f"Loading conversation history for {self.actor_id}")

            # Load recent conversation turns from AgentCore memory
            recent_turns = self.memory_client.get_last_k_turns(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                k=20,  # Last 20 turns for context
                branch_name="main"
            )
            
            if recent_turns and recent_turns.get('turns'):
                logger.info(f"Loaded {len(recent_turns['turns'])} conversation turns")
                
                # Add conversation history to agent's message history
                for turn in recent_turns['turns']:
                    if turn.get('messages'):
                        for message in turn['messages']:
                            content = message.get('content', '')
                            role = message.get('role', 'user')
                            
                            # Add to agent's conversation history
                            event.agent.messages.append({
                                "role": role,
                                "content": [{"text": content, "type": "text"}]
                            })
            else:
                logger.info("No previous conversation history found")
                
        except Exception as e:
            logger.error(f"Failed to load conversation history for {self.actor_id}: {e}")
    
    def on_message_added(self, event: MessageAddedEvent):
        """Store conversation turns in AgentCore memory."""
        try:
            messages = event.agent.messages
            if not messages:
                return
                
            # Get the latest message
            latest_message = messages[-1]
            content = latest_message.get("content", [{}])[0].get("text", "")
            role = latest_message.get("role", "assistant")
            
            logger.info(f"Storing message in memory for {self.actor_id}")
            
            # Store in AgentCore memory
            self.memory_client.create_event(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                messages=[(content, role)]
            )
            
            logger.info(f"Successfully stored message for {self.actor_id}")
            
        except Exception as e:
            logger.error(f"Failed to store message for {self.actor_id}: {e}")
    
    def register_hooks(self, registry: HookRegistry) -> None:
        """Register the memory hooks with the hook registry."""
        registry.add_callback(MessageAddedEvent, self.on_message_added)
        registry.add_callback(AgentInitializedEvent, self.on_agent_initialized)
