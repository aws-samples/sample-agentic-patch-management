"""
Workflow event emitter for real-time UI tracking.

Provides a thread-safe, session-scoped event queue that specialist agents
push structured events into via a Strands hook. The SSE endpoint drains
these events and forwards them to the browser alongside the main stream.

Architecture:
  specialist tool call → WorkflowTrackingHook → WorkflowEventEmitter (queue)
  SSE endpoint polls queue → injects workflow SSE events into response stream
"""

import json
import logging
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from queue import Queue, Empty
from typing import Optional

from strands.hooks import HookProvider, HookRegistry, MessageAddedEvent

logger = logging.getLogger(__name__)

# Tool name → workflow step ID mapping
TOOL_STEP_MAP: dict[str, str] = {
    "get_patch_compliance": "discovery",
    "get_vulnerability_findings": "discovery",
    "get_maintenance_windows": "window_check",
    "execute_patch_operation": "patch_execute",
    "get_command_status": "patch_execute",
    "check_instance_health": "health_check",
    "check_cloudwatch_alarms": "alarm_check",
    "generate_compliance_report": "report",
    "verify_and_proceed": "verification",
    "rollback_patches": "patch_execute",
    "get_resource_dependencies": "discovery",
}


@dataclass
class WorkflowEvent:
    """A single workflow event to be sent to the UI."""
    type: str  # workflow_start, workflow_step, workflow_data, workflow_env_done
    timestamp: float = field(default_factory=time.time)
    # Payload fields (optional, vary by event type)
    step: Optional[str] = None
    tool: Optional[str] = None
    status: Optional[str] = None
    data: Optional[dict] = None
    environment: Optional[str] = None
    cve_id: Optional[str] = None
    severity: Optional[str] = None
    environments: Optional[list] = None

    def to_sse(self) -> str:
        """Serialize to SSE data line."""
        payload = {"type": self.type}
        for k in ("step", "tool", "status", "data", "environment",
                   "cve_id", "severity", "environments"):
            v = getattr(self, k)
            if v is not None:
                payload[k] = v
        return f"data: {json.dumps(payload)}\n\n"


class WorkflowEventEmitter:
    """Thread-safe, session-scoped event queue.

    Each session gets its own queue. Specialist agents push events via
    `emit()`, and the SSE endpoint drains via `drain()`.
    """

    def __init__(self):
        self._queues: dict[str, Queue] = defaultdict(Queue)
        self._lock = threading.Lock()
        self._started: dict[str, bool] = {}  # Track workflow_start per session

    def emit(self, session_id: str, event: WorkflowEvent) -> None:
        """Push an event onto the session's queue."""
        with self._lock:
            self._queues[session_id].put(event)
        logger.debug(f"[workflow] session={session_id} event={event.type} step={event.step}")

    def drain(self, session_id: str, timeout: float = 0.05) -> list[WorkflowEvent]:
        """Drain all pending events for a session (non-blocking)."""
        events = []
        with self._lock:
            q = self._queues.get(session_id)
        if not q:
            return events
        while True:
            try:
                events.append(q.get_nowait())
            except Empty:
                break
        return events

    def cleanup(self, session_id: str) -> None:
        """Remove a session's queue after the stream ends."""
        with self._lock:
            self._queues.pop(session_id, None)
            self._started.pop(session_id, None)

    def mark_started(self, session_id: str) -> bool:
        """Mark workflow as started for session. Returns True if this is the first call."""
        with self._lock:
            if self._started.get(session_id):
                return False
            self._started[session_id] = True
            return True


# Global singleton — shared between agent process and SSE endpoint
_emitter = WorkflowEventEmitter()


def get_emitter() -> WorkflowEventEmitter:
    return _emitter


class WorkflowTrackingHook(HookProvider):
    """Strands hook that intercepts tool calls on specialist agents and emits
    structured workflow events to the shared emitter.

    Attach to specialist agents (patch_manager, vulnerability_analyst, etc.)
    via the agent factory.
    """

    def __init__(self, session_id: str, agent_name: str):
        self.session_id = session_id
        self.agent_name = agent_name
        self._emitter = get_emitter()

    def on_message_added(self, event: MessageAddedEvent) -> None:
        """Intercept messages to detect tool use and tool results."""
        msg = event.message
        role = msg.get("role", "")
        content_blocks = msg.get("content", [])

        if role == "assistant":
            # Assistant messages may contain toolUse blocks (tool invocations)
            for block in content_blocks:
                if "toolUse" in block:
                    tool_use = block["toolUse"]
                    tool_name = tool_use.get("name", "")
                    tool_input = tool_use.get("input", {})
                    step = TOOL_STEP_MAP.get(tool_name)
                    if not step:
                        continue

                    # Emit workflow_start on first workflow tool
                    if self._emitter.mark_started(self.session_id):
                        start_event = WorkflowEvent(type="workflow_start")
                        # Extract CVE/severity/environment from tool input
                        if isinstance(tool_input, dict):
                            cve = tool_input.get("cve_id")
                            sev = tool_input.get("severity")
                            env = tool_input.get("environment")
                            if cve:
                                start_event.cve_id = cve
                            if sev:
                                start_event.severity = sev
                            if env:
                                start_event.environments = [env]
                        self._emitter.emit(self.session_id, start_event)

                    # Emit workflow_step started
                    self._emitter.emit(self.session_id, WorkflowEvent(
                        type="workflow_step",
                        step=step,
                        tool=tool_name,
                        status="started",
                    ))

        elif role == "user":
            # "user" role messages after tool calls contain toolResult blocks
            for block in content_blocks:
                if "toolResult" in block:
                    tool_result = block["toolResult"]
                    tool_use_id = tool_result.get("toolUseId", "")
                    result_content = tool_result.get("content", [])
                    result_status = tool_result.get("status", "success")

                    # Find the matching tool name from recent assistant messages
                    tool_name = self._find_tool_name(event.agent.messages, tool_use_id)
                    if not tool_name:
                        continue
                    step = TOOL_STEP_MAP.get(tool_name)
                    if not step:
                        continue

                    # Parse the result JSON
                    result_data = self._parse_result(result_content)

                    # Emit workflow_step complete
                    self._emitter.emit(self.session_id, WorkflowEvent(
                        type="workflow_step",
                        step=step,
                        tool=tool_name,
                        status="complete" if result_status == "success" else "failed",
                    ))

                    # Emit workflow_data with the tool result
                    if result_data:
                        self._emitter.emit(self.session_id, WorkflowEvent(
                            type="workflow_data",
                            step=step,
                            tool=tool_name,
                            data=result_data,
                        ))

                    # Emit workflow_env_done when compliance report completes
                    if tool_name == "generate_compliance_report" and result_data:
                        env_name = result_data.get("environment", "unknown")
                        self._emitter.emit(self.session_id, WorkflowEvent(
                            type="workflow_env_done",
                            environment=env_name,
                        ))

    def _find_tool_name(self, messages: list, tool_use_id: str) -> Optional[str]:
        """Walk backwards through messages to find the tool name for a toolUseId."""
        for msg in reversed(messages):
            if msg.get("role") != "assistant":
                continue
            for block in msg.get("content", []):
                if "toolUse" in block:
                    if block["toolUse"].get("toolUseId") == tool_use_id:
                        return block["toolUse"].get("name")
        return None

    def _parse_result(self, content_blocks: list) -> Optional[dict]:
        """Extract JSON data from tool result content blocks."""
        for block in content_blocks:
            if isinstance(block, dict) and "text" in block:
                try:
                    return json.loads(block["text"])
                except (json.JSONDecodeError, TypeError):
                    pass
            elif isinstance(block, dict) and "json" in block:
                return block["json"]
        return None

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(MessageAddedEvent, self.on_message_added)
