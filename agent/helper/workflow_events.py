"""
Workflow event emitter for real-time UI tracking.

Uses SQS FIFO as a cross-process event transport so that workflow events
emitted by specialist agents (running inside AgentCore containers) can be
consumed by the FastAPI SSE endpoint (running locally or in a separate process).

Architecture:
  specialist tool call -> WorkflowTrackingHook -> SQS FIFO (send_message)
  SSE endpoint polls SQS -> injects workflow SSE events into response stream

SQS queue: patch-automation-workflow-events.fifo
  MessageGroupId: session_id  (ensures per-session ordering)
  MessageDeduplicationId: uuid (unique per event)
  Auto-deletes after receive + delete
"""

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from strands.hooks import HookProvider, HookRegistry, MessageAddedEvent

logger = logging.getLogger(__name__)

# SQS queue name
WORKFLOW_QUEUE_NAME = "patch-automation-workflow-events.fifo"

# Tool name -> workflow step ID mapping
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
    step: Optional[str] = None
    tool: Optional[str] = None
    status: Optional[str] = None
    data: Optional[dict] = None
    environment: Optional[str] = None
    cve_id: Optional[str] = None
    severity: Optional[str] = None
    environments: Optional[list] = None
    error_message: Optional[str] = None

    def to_sse(self) -> str:
        """Serialize to SSE data line."""
        payload = {"type": self.type}
        for k in ("step", "tool", "status", "data", "environment",
                   "cve_id", "severity", "environments", "error_message"):
            v = getattr(self, k)
            if v is not None:
                payload[k] = v
        return f"data: {json.dumps(payload)}\n\n"

    def to_sqs_body(self) -> str:
        """Serialize to SQS message body (JSON string)."""
        payload: dict = {"type": self.type, "timestamp": self.timestamp}
        for k in ("step", "tool", "status", "environment",
                   "cve_id", "severity", "error_message"):
            v = getattr(self, k)
            if v is not None:
                payload[k] = v
        if self.data is not None:
            payload["data"] = self.data
        if self.environments is not None:
            payload["environments"] = self.environments
        return json.dumps(payload)

    @classmethod
    def from_sqs_body(cls, body: str) -> "WorkflowEvent":
        """Deserialize from SQS message body."""
        d = json.loads(body)
        return cls(
            type=d["type"],
            timestamp=d.get("timestamp", time.time()),
            step=d.get("step"),
            tool=d.get("tool"),
            status=d.get("status"),
            data=d.get("data"),
            environment=d.get("environment"),
            cve_id=d.get("cve_id"),
            severity=d.get("severity"),
            environments=d.get("environments"),
            error_message=d.get("error_message"),
        )


def _get_sqs_client():
    """Create an SQS client using environment config."""
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    config = BotoConfig(
        retries={"max_attempts": 2, "mode": "standard"},
        connect_timeout=5,
        read_timeout=5,
    )
    return boto3.client("sqs", region_name=region, config=config)


def _get_queue_url(client) -> Optional[str]:
    """Resolve the SQS FIFO queue URL. Returns None if queue doesn't exist."""
    try:
        resp = client.get_queue_url(QueueName=WORKFLOW_QUEUE_NAME)
        return resp["QueueUrl"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "AWS.SimpleQueueService.NonExistentQueue":
            logger.warning(f"[workflow] SQS queue {WORKFLOW_QUEUE_NAME} not found. "
                           "Deploy infra first: ./deploy.sh infra")
            return None
        logger.warning(f"[workflow] Failed to get queue URL: {e}")
        return None


class WorkflowEventEmitter:
    """SQS FIFO-backed event emitter for cross-process workflow tracking.

    The agent process (running in AgentCore) writes events via emit().
    The SSE endpoint (running in FastAPI) reads events via drain().
    SQS FIFO provides ordered, exactly-once delivery per session.
    """

    def __init__(self):
        self._client = None
        self._queue_url: Optional[str] = None
        self._queue_checked = False
        self._started: dict[str, bool] = {}

    def _ensure_client(self) -> bool:
        """Lazily initialize SQS client and resolve queue URL."""
        if self._client is None:
            self._client = _get_sqs_client()
        if not self._queue_checked:
            self._queue_url = _get_queue_url(self._client)
            self._queue_checked = True
        return self._queue_url is not None

    def emit(self, session_id: str, event: WorkflowEvent) -> None:
        """Send an event to SQS for the given session."""
        try:
            if not self._ensure_client():
                return
            self._client.send_message(
                QueueUrl=self._queue_url,
                MessageBody=event.to_sqs_body(),
                MessageGroupId=session_id,
                MessageDeduplicationId=uuid.uuid4().hex,
            )
            logger.debug(f"[workflow] session={session_id} event={event.type} step={event.step}")
        except Exception as e:
            # Never let workflow tracking break the agent
            logger.warning(f"[workflow] Failed to emit event: {e}")

    def drain(self, session_id: str) -> list[WorkflowEvent]:
        """Receive and delete all available events for any session.

        SQS FIFO with MessageGroupId ensures per-session ordering.
        We receive up to 10 messages at a time (SQS max) with short polling.
        """
        events: list[WorkflowEvent] = []
        try:
            if not self._ensure_client():
                return events

            resp = self._client.receive_message(
                QueueUrl=self._queue_url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=1,
                AttributeNames=["MessageGroupId"],
            )

            messages = resp.get("Messages", [])
            if not messages:
                return events

            entries = []
            for msg in messages:
                try:
                    evt = WorkflowEvent.from_sqs_body(msg["Body"])
                    events.append(evt)
                except Exception as e:
                    logger.warning(f"[workflow] Failed to parse SQS message: {e}")
                # Always delete (including malformed) to avoid poison pills
                entries.append({
                    "Id": msg["MessageId"],
                    "ReceiptHandle": msg["ReceiptHandle"],
                })

            if entries:
                self._client.delete_message_batch(
                    QueueUrl=self._queue_url,
                    Entries=entries,
                )

        except Exception as e:
            logger.warning(f"[workflow] Failed to drain events: {e}")

        return events

    def cleanup(self, session_id: str) -> None:
        """Drain any remaining messages for a session."""
        try:
            remaining = self.drain(session_id)
            if remaining:
                logger.debug(f"[workflow] Cleaned up {len(remaining)} remaining events")
        except Exception as e:
            logger.warning(f"[workflow] Failed to cleanup session: {e}")

    def mark_started(self, session_id: str) -> bool:
        """Track whether workflow_start has been emitted for this session.

        Uses in-process dict since this runs within a single agent invocation.
        The agent container handles one request at a time.
        """
        if self._started.get(session_id):
            return False
        self._started[session_id] = True
        return True


# Module-level singleton
_emitter: Optional[WorkflowEventEmitter] = None


def get_emitter() -> WorkflowEventEmitter:
    global _emitter
    if _emitter is None:
        _emitter = WorkflowEventEmitter()
    return _emitter


class WorkflowTrackingHook(HookProvider):
    """Strands hook that intercepts tool calls on specialist agents and emits
    structured workflow events to SQS via the shared emitter.

    Attach to specialist agents (patch_manager, vulnerability_analyst, etc.)
    via the agent factory. Not attached to the supervisor since its tool calls
    (consult_*) are already visible in the outer SSE stream.
    """

    def __init__(self, session_id: str, agent_name: str):
        self.session_id = session_id
        self.agent_name = agent_name
        self._emitter = get_emitter()
        logger.info(f"[workflow] Hook attached: agent={agent_name} session={session_id}")

    def on_message_added(self, event: MessageAddedEvent) -> None:
        """Intercept messages to detect tool use and tool results."""
        try:
            self._process_message(event)
        except Exception as e:
            # Never let hook errors break the agent
            logger.warning(f"[workflow] Hook error in on_message_added: {e}", exc_info=True)

    def _process_message(self, event: MessageAddedEvent) -> None:
        """Internal message processing with defensive parsing."""
        msg = event.message
        if not isinstance(msg, dict):
            return

        role = msg.get("role", "")
        content_blocks = msg.get("content")
        if not isinstance(content_blocks, list):
            return

        if role == "assistant":
            self._handle_assistant_message(content_blocks)
        elif role == "user":
            self._handle_user_message(content_blocks, event)

    def _handle_assistant_message(self, content_blocks: list) -> None:
        """Process assistant messages containing toolUse blocks."""
        for block in content_blocks:
            if not isinstance(block, dict) or "toolUse" not in block:
                continue
            tool_use = block["toolUse"]
            if not isinstance(tool_use, dict):
                continue

            tool_name = tool_use.get("name", "")
            tool_input = tool_use.get("input", {})
            if not isinstance(tool_input, dict):
                tool_input = {}

            step = TOOL_STEP_MAP.get(tool_name)
            if not step:
                continue

            # Emit workflow_start on first workflow tool
            if self._emitter.mark_started(self.session_id):
                start_event = WorkflowEvent(type="workflow_start")
                cve = tool_input.get("cve_id")
                sev = tool_input.get("severity")
                env = tool_input.get("environment")
                if isinstance(cve, str):
                    start_event.cve_id = cve
                if isinstance(sev, str):
                    start_event.severity = sev
                if isinstance(env, str):
                    start_event.environments = [env]
                self._emitter.emit(self.session_id, start_event)

            # Emit workflow_step started
            self._emitter.emit(self.session_id, WorkflowEvent(
                type="workflow_step",
                step=step,
                tool=tool_name,
                status="started",
            ))

    def _handle_user_message(self, content_blocks: list, event: MessageAddedEvent) -> None:
        """Process user messages containing toolResult blocks."""
        for block in content_blocks:
            if not isinstance(block, dict) or "toolResult" not in block:
                continue
            tool_result = block["toolResult"]
            if not isinstance(tool_result, dict):
                continue

            tool_use_id = tool_result.get("toolUseId", "")
            result_content = tool_result.get("content", [])
            result_status = tool_result.get("status", "success")

            if not isinstance(result_content, list):
                result_content = []

            # Find the matching tool name
            agent_obj = getattr(event, "agent", None)
            messages = getattr(agent_obj, "messages", []) if agent_obj else []
            tool_name = self._find_tool_name(messages, tool_use_id)
            if not tool_name:
                continue
            step = TOOL_STEP_MAP.get(tool_name)
            if not step:
                continue

            # Parse the result JSON
            result_data = self._parse_result(result_content)

            # Include error details for failed tools
            error_msg = None
            if result_status != "success" and result_data:
                raw_err = result_data.get("error", result_data.get("message"))
                if raw_err is not None:
                    error_msg = str(raw_err) if not isinstance(raw_err, str) else raw_err

            # Emit workflow_step complete/failed
            self._emitter.emit(self.session_id, WorkflowEvent(
                type="workflow_step",
                step=step,
                tool=tool_name,
                status="complete" if result_status == "success" else "failed",
                error_message=error_msg,
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
        if not isinstance(messages, list):
            return None
        for msg in reversed(messages):
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            for block in msg.get("content", []):
                if not isinstance(block, dict) or "toolUse" not in block:
                    continue
                tu = block["toolUse"]
                if isinstance(tu, dict) and tu.get("toolUseId") == tool_use_id:
                    return tu.get("name")
        return None

    def _parse_result(self, content_blocks: list) -> Optional[dict]:
        """Extract JSON data from tool result content blocks."""
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            if "text" in block:
                try:
                    return json.loads(block["text"])
                except (json.JSONDecodeError, TypeError):
                    pass
            elif "json" in block:
                val = block["json"]
                if isinstance(val, dict):
                    return val
        return None

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(MessageAddedEvent, self.on_message_added)
