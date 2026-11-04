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


def usage_event(result: Any, elapsed_ms: int, first_text_ms: Optional[int]) -> dict:
    """Summarise one invocation's token use and timing for the caller and logs.

    Args:
        result: The AgentResult from the stream, or None if none arrived.
        elapsed_ms: Wall-clock time for the invocation.
        first_text_ms: Time until the first answer text, or None.

    Returns:
        {'usage': {...}} with counts and timings only.
    """
    metrics = getattr(result, "metrics", None)
    usage = getattr(metrics, "accumulated_usage", None) or {}
    tool_metrics = getattr(metrics, "tool_metrics", None) or {}
    return {"usage": {
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "cache_read_tokens": usage.get("cacheReadInputTokens"),
        "cache_write_tokens": usage.get("cacheWriteInputTokens"),
        "model_calls": getattr(metrics, "cycle_count", None),
        "tools": {name: getattr(m, "call_count", 0) for name, m in tool_metrics.items()},
        "tool_errors": sum(getattr(m, "error_count", 0) for m in tool_metrics.values()),
        "stop_reason": getattr(result, "stop_reason", None),
        "elapsed_ms": elapsed_ms,
        "first_text_ms": first_text_ms,
    }}
