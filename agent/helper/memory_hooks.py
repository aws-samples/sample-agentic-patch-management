"""
Read-only memory hook for specialist agents.

The supervisor uses AgentCoreMemorySessionManager (the official Strands integration)
for full STM persistence and LTM retrieval. Specialists only need to READ the
supervisor's conversation context without writing to memory — this hook handles that.
"""

import re
import logging
from botocore.config import Config as BotocoreConfig
from strands.hooks import AgentInitializedEvent, HookProvider, HookRegistry
from bedrock_agentcore.memory import MemoryClient

logger = logging.getLogger(__name__)

_RETRY_CONFIG = BotocoreConfig(
    retries={"max_attempts": 5, "mode": "adaptive"}
)


def _sanitize_actor_id(actor_id: str) -> str:
    """Ensure actor_id matches [a-zA-Z0-9][a-zA-Z0-9-_/]* — strip invalid chars like '@'."""
    sanitized = re.sub(r'[^a-zA-Z0-9\-_/]', '-', actor_id)
    sanitized = re.sub(r'-{2,}', '-', sanitized).strip('-')
    return sanitized or "anonymous"


class ReadOnlyMemoryHook(HookProvider):
    """Loads the supervisor's conversation history into a specialist agent.

    Specialists are stateless — created per-request, invoked once, discarded.
    They need the supervisor's recent conversation context to understand what
    the operator has been discussing, but they must NOT write to memory
    (that would pollute the supervisor's conversation history).

    This hook loads the last K turns from the supervisor's session on agent init.
    """

    def __init__(self, memory_id: str, actor_id: str, session_id: str, k: int = 10):
        self.memory_id = memory_id
        self.actor_id = _sanitize_actor_id(actor_id)
        self.session_id = session_id
        self.k = k
        self._memory_client = MemoryClient()
        logger.info(f"Read-only memory hook: actor={self.actor_id}, session={session_id}")

    def on_agent_initialized(self, event: AgentInitializedEvent):
        """Load recent conversation turns from the supervisor's session."""
        try:
            recent_turns = self._memory_client.get_last_k_turns(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                k=self.k,
                branch_name="main"
            )

            if not recent_turns or not isinstance(recent_turns, list):
                logger.info("No conversation history found for specialist context")
                return

            count = 0
            for turn in recent_turns:
                if not isinstance(turn, list):
                    continue
                for message in turn:
                    if not isinstance(message, dict):
                        continue
                    content_obj = message.get('content', {})
                    text = content_obj.get('text', '') if isinstance(content_obj, dict) else str(content_obj)
                    role = message.get('role', 'user').lower()

                    if text.strip():
                        event.agent.messages.append({
                            "role": role,
                            "content": [{"text": text, "type": "text"}]
                        })
                        count += 1

            logger.info(f"Loaded {count} messages as specialist context")

        except Exception as e:
            logger.warning(f"Failed to load specialist context: {e}")

    def register_hooks(self, registry: HookRegistry) -> None:
        """Register the read-only init hook (no message-added hook — we don't write)."""
        registry.add_callback(AgentInitializedEvent, self.on_agent_initialized)
