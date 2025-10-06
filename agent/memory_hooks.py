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
        logger.info(f"Memory hook initialized for {actor_id} with memory_id: {memory_id}")
    
    def on_agent_initialized(self, event: AgentInitializedEvent):
        """Load recent conversation history when agent starts."""
        try:
            logger.info(f"Loading conversation history for {self.actor_id}")

            # Load recent conversation turns from AgentCore memory
            recent_turns = self.memory_client.get_last_k_turns(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                k=10,  # Last 10 turns for context
                branch_name="main"
            )
            
            if recent_turns and isinstance(recent_turns, list):
                logger.info(f"Loaded {len(recent_turns)} conversation turns")
                
                # Process each turn - each turn is a list of messages
                for turn in recent_turns:
                    if isinstance(turn, list):
                        for message in turn:
                            if isinstance(message, dict):
                                # Extract content and role from AgentCore format
                                content_obj = message.get('content', {})
                                if isinstance(content_obj, dict):
                                    content = content_obj.get('text', '')
                                else:
                                    content = str(content_obj)
                                
                                role = message.get('role', 'user').lower()
                                
                                if content.strip():
                                    # Add to agent's conversation history
                                    event.agent.messages.append({
                                        "role": role,
                                        "content": [{"text": content, "type": "text"}]
                                    })
                                    logger.info(f"Added message: role={role}, content={content[:50]}...")
                            
                logger.info(f"Total agent messages after loading: {len(event.agent.messages)}")
            else:
                logger.info("No previous conversation history found")
                
        except Exception as e:
            logger.warning(f"Failed to load conversation history for {self.actor_id}: {e}")
            import traceback
            logger.warning(f"Traceback: {traceback.format_exc()}")
    
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
            
            if not content.strip():
                return
                
            logger.info(f"Storing message in memory for {self.actor_id}: {content[:50]}...")
            
            # Store in AgentCore memory
            self.memory_client.create_event(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                messages=[(content, role)]
            )
            
            logger.info(f"Successfully stored message for {self.actor_id}")
            
        except Exception as e:
            logger.warning(f"Failed to store message for {self.actor_id}: {e}")
    
    def register_hooks(self, registry: HookRegistry) -> None:
        """Register the memory hooks with the hook registry."""
        logger.info(f"Registering memory hooks for {self.actor_id}")
        registry.add_callback(MessageAddedEvent, self.on_message_added)
        registry.add_callback(AgentInitializedEvent, self.on_agent_initialized)
