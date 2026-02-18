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
    Supports read-write (supervisor) and read-only (specialist) modes.
    Retrieves long-term memories (semantic + summary) for cross-session context.
    """
    
    def __init__(self, memory_client: MemoryClient, memory_id: str, actor_id: str, session_id: str,
                 read_only: bool = False):
        """
        Initialize the memory hook provider.
        
        Args:
            memory_client: AgentCore memory client instance
            memory_id: AgentCore memory resource ID  
            actor_id: Operator-scoped identifier (e.g., "patch-automation/operator@web-abc123")
            session_id: Session identifier for conversation isolation
            read_only: If True, load history on init but don't store new messages.
                       Used by specialists to read supervisor context without polluting it.
        """
        self.memory_client = memory_client
        self.memory_id = memory_id
        self.actor_id = actor_id
        self.session_id = session_id
        self.read_only = read_only
        self._first_user_message = None  # Captured for semantic retrieval
        mode_label = "read-only" if read_only else "read-write"
        logger.info(f"Memory hook initialized for {actor_id} ({mode_label}) with memory_id: {memory_id}")
    
    def on_agent_initialized(self, event: AgentInitializedEvent):
        """Load conversation history and long-term memories when agent starts."""
        try:
            logger.info(f"Loading conversation history for {self.actor_id}")

            # 1. Load recent conversation turns from current session (short-term memory)
            recent_turns = self.memory_client.get_last_k_turns(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                k=10,
                branch_name="main"
            )
            
            if recent_turns and isinstance(recent_turns, list):
                logger.info(f"Loaded {len(recent_turns)} conversation turns")
                
                for turn in recent_turns:
                    if isinstance(turn, list):
                        for message in turn:
                            if isinstance(message, dict):
                                content_obj = message.get('content', {})
                                if isinstance(content_obj, dict):
                                    content = content_obj.get('text', '')
                                else:
                                    content = str(content_obj)
                                
                                role = message.get('role', 'user').lower()
                                
                                if content.strip():
                                    # Capture first user message for semantic retrieval
                                    if role == 'user' and self._first_user_message is None:
                                        self._first_user_message = content
                                    
                                    event.agent.messages.append({
                                        "role": role,
                                        "content": [{"text": content, "type": "text"}]
                                    })
                                    logger.info(f"Added message: role={role}, content={content[:50]}...")
                            
                logger.info(f"Total agent messages after loading: {len(event.agent.messages)}")
            else:
                logger.info("No previous conversation history found")
            
            # 2. Retrieve long-term memories (cross-session context) — GAP-3
            # Only for supervisor (read-write mode). Specialists get context from
            # the supervisor's conversation history which already includes this.
            if not self.read_only:
                self._inject_long_term_memories(event)
                
        except Exception as e:
            logger.warning(f"Failed to load conversation history for {self.actor_id}: {e}")
            import traceback
            logger.warning(f"Traceback: {traceback.format_exc()}")
    
    def _inject_long_term_memories(self, event: AgentInitializedEvent):
        """Retrieve and inject long-term memories as context for cross-session recall.
        
        Searches semantic memory using the first user message (or a generic query)
        to find relevant facts from previous sessions. Injected as a system-context
        message at the start of the conversation.
        """
        try:
            # Build search query from first user message or generic
            search_query = self._first_user_message or "patch automation vulnerability compliance"
            
            # Retrieve semantic memories scoped to this operator
            # Namespace pattern: /actor/{actorId}/strategy/{strategyId}/{sessionId}
            # We search at the actor level to get cross-session results
            semantic_namespace = f"/actor/{self.actor_id}"
            
            memories = self.memory_client.retrieve_memories(
                memory_id=self.memory_id,
                namespace=semantic_namespace,
                query=search_query,
                top_k=5
            )
            
            if memories:
                # Format memories as context
                memory_lines = []
                for mem in memories:
                    content = mem.get('content', {})
                    if isinstance(content, dict):
                        text = content.get('text', '')
                    else:
                        text = str(content)
                    if text.strip():
                        memory_lines.append(f"- {text}")
                
                if memory_lines:
                    context_text = (
                        "CONTEXT FROM PREVIOUS SESSIONS (long-term memory):\n"
                        + "\n".join(memory_lines[:10])  # Cap at 10 facts
                        + "\n\nUse this context to provide continuity. "
                        "Do not repeat these facts unless relevant to the current query."
                    )
                    
                    # Inject as the first message (before conversation history)
                    event.agent.messages.insert(0, {
                        "role": "user",
                        "content": [{"text": context_text, "type": "text"}]
                    })
                    event.agent.messages.insert(1, {
                        "role": "assistant",
                        "content": [{"text": "Understood. I have context from your previous sessions and will use it as needed.", "type": "text"}]
                    })
                    
                    logger.info(f"Injected {len(memory_lines)} long-term memories as context")
            else:
                logger.info("No long-term memories found for this operator")
                
        except Exception as e:
            # Long-term memory retrieval is best-effort — don't break the agent
            logger.warning(f"Failed to retrieve long-term memories: {e}")
    
    def on_message_added(self, event: MessageAddedEvent):
        """Store conversation turns in AgentCore memory (skipped in read-only mode)."""
        if self.read_only:
            return
        
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
