"""Reduce Strands stream events to what the runtime's caller needs."""

from typing import Any, Optional


def client_event(event: Any) -> Optional[dict]:
    """Return the client-safe form of a Strands stream event, or None to drop it.

    Args:
        event: One event from Agent.stream_async.

    Returns:
        A tool-start event carrying only the tool name, a text-delta event, or None.
    """
    if not isinstance(event, dict):
        return None
    inner = event.get("event")
    if not isinstance(inner, dict):
        return None

    start = inner.get("contentBlockStart", {}).get("start", {})
    tool_name = start.get("toolUse", {}).get("name") if isinstance(start, dict) else None
    if tool_name:
        return {"event": {"contentBlockStart": {"start": {"toolUse": {"name": tool_name}}}}}

    delta = inner.get("contentBlockDelta", {}).get("delta", {})
    text = delta.get("text") if isinstance(delta, dict) else None
    if isinstance(text, str):
        return {"event": {"contentBlockDelta": {"delta": {"text": text}}}}

    return None
