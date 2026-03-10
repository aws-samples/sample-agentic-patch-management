"""Helper to extract relevant context from supervisor memory"""

import re
from bedrock_agentcore.memory import MemoryClient
import logging

logger = logging.getLogger(__name__)


def _build_actor_id(operator_id: str | None) -> str:
    """Build operator-scoped actor_id — must match agent_factory._build_actor_id()."""
    base = operator_id or "anonymous"
    sanitized = re.sub(r'[^a-zA-Z0-9\-_/]', '-', base)
    sanitized = re.sub(r'-{2,}', '-', sanitized).strip('-')
    return f"patch-automation/{sanitized}" if sanitized else "patch-automation/anonymous"


def get_relevant_context(memory_id: str, session_id: str, operator_id: str | None = None,
                         max_turns: int = 3) -> str:
    """
    Extract recent conversation context from supervisor's memory.

    Args:
        memory_id: Memory resource ID
        session_id: Current session ID
        operator_id: Operator identity (used to compute actor_id for memory lookup)
        max_turns: Number of recent turns to include (default 3)

    Returns:
        Formatted context string with recent conversation
    """
    if not memory_id:
        logger.info("No memory_id provided - skipping context retrieval")
        return ""

    try:
        actor_id = _build_actor_id(operator_id)
        logger.info(f"Loading context from memory: {memory_id}, session: {session_id}, actor: {actor_id}")
        memory_client = MemoryClient()

        recent_turns = memory_client.get_last_k_turns(
            memory_id=memory_id,
            actor_id=actor_id,
            session_id=session_id,
            k=max_turns,
            branch_name="main"
        )

        if not recent_turns:
            logger.info("No previous conversation history found in memory")
            return ""

        logger.info(f"Loaded {len(recent_turns)} turns from memory")

        # Format context
        context_parts = []
        for turn in recent_turns:
            if isinstance(turn, list):
                for message in turn:
                    if isinstance(message, dict):
                        content_obj = message.get('content', {})
                        content = content_obj.get('text', '') if isinstance(content_obj, dict) else str(content_obj)
                        role = message.get('role', 'user')

                        if content.strip():
                            context_parts.append(f"{role.upper()}: {content.strip()}")

        context_str = "\n".join(context_parts[-6:]) if context_parts else ""
        logger.info(f"Context prepared: {len(context_str)} chars, {len(context_parts)} messages")
        return context_str

    except Exception as e:
        logger.warning(f"Failed to load context from memory: {e}")
        return ""
