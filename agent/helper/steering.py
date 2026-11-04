"""Deterministic steering handlers for workflow enforcement.

Replaces prompt-based rules with code-enforced hooks that fire just-in-time
before tool calls and after model responses. Uses LedgerProvider to track
tool call history for workflow ordering checks.

Two handlers:
- PatchWorkflowSteering: patch workflow ordering + safety
- ComplianceOutputSteering: prevents dangerous zero-data misrepresentation
"""

import logging
import re
from typing import Any

from strands.vended_plugins.steering import (
    Guide,
    LedgerProvider,
    ModelSteeringAction,
    Proceed,
    SteeringHandler,
    ToolSteeringAction,
)
from strands.types.content import Message
from strands.types.streaming import StopReason
from strands.types.tools import ToolUse

logger = logging.getLogger(__name__)


def _get_ledger(steering_context) -> dict:
    """Get the tool call ledger from steering context."""
    context_data = steering_context.data.get() or {}
    return context_data.get("ledger", {})


def _completed_tool_names(steering_context) -> list[str]:
    """Extract names of successfully completed tools from the ledger."""
    ledger = _get_ledger(steering_context)
    return [
        c["tool_name"]
        for c in ledger.get("tool_calls", [])
        if c.get("status") == "success"
    ]


def _last_tool_call(steering_context, tool_name: str) -> dict | None:
    """Find the most recent completed call for a tool name."""
    ledger = _get_ledger(steering_context)
    for call in reversed(ledger.get("tool_calls", [])):
        if call.get("tool_name") == tool_name and call.get("status") == "success":
            return call
    return None


_CVE_RE = re.compile(r'CVE-\d{4}-\d{4,7}', re.IGNORECASE)
_INSTANCE_ID_RE = re.compile(r'\bi-[0-9a-f]{8,17}\b', re.IGNORECASE)
# A request that only reads, even when it mentions patching: "Verify health of patch run ... on i-...".
_READ_REQUEST_RE = re.compile(r'(verify|check|show|list|status|did|has|have|is|are|what|which|how|who|when|get)\b',
                              re.IGNORECASE)
# Patching named as a thing, not asked for: a patch run, its state, compliance, plan or report.
_PATCH_NOUN_RE = re.compile(r'\bpatch(?:es|ing)?\s+(?:run|state|status|compliance|level|plan|report|window|policy)s?\b',
                            re.IGNORECASE)

def _last_user_text(agent) -> str:
    """Text of the operator's most recent message, skipping tool-result messages."""
    try:
        messages = getattr(agent, "messages", None) or []
    except Exception:
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content") or []
        blocks = content if isinstance(content, list) else [content]
        if blocks and all(isinstance(b, dict) and "toolResult" in b for b in blocks):
            continue
        for block in blocks:
            text = block.get("text") if isinstance(block, dict) else str(block)
            if text:
                return text
        return ""
    return ""


def _operator_turn(agent) -> int:
    """Position of the operator's latest message (tool-result messages skipped): one value per turn."""
    messages = getattr(agent, "messages", None) or []
    for n in range(len(messages) - 1, -1, -1):
        msg = messages[n]
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content") or []
        blocks = content if isinstance(content, list) else [content]
        if blocks and all(isinstance(b, dict) and "toolResult" in b for b in blocks):
            continue
        return n
    return -1


# After-model Guides make Strands retry the model with no limit of its own, so each
# handler gets at most this many per operator message; then the answer goes through.
_GUIDES_PER_TURN = 2


class _GuideBudget:
    def __init__(self):
        self._turn, self._used = None, 0

    def spend(self, agent) -> bool:
        turn = _operator_turn(agent)
        if turn != self._turn:
            self._turn, self._used = turn, 0
        if self._used >= _GUIDES_PER_TURN:
            return False
        self._used += 1
        return True


# A question about an instance ("why did the patch on i-... fail?") isn't a request to patch it,
# but "can you patch i-... in dev?" is.
_INTERROGATIVE_RE = re.compile(r"^\s*(why|what|how|did|does|do|is|was|were|when|which|who|whose|has|have)\b", re.IGNORECASE)
_POLITE_REQUEST_RE = re.compile(r"^\s*(please\b|(can|could|would|will) you\b)", re.IGNORECASE)


def _is_question(text: str) -> bool:
    if _POLITE_REQUEST_RE.search(text):
        return False
    return bool(_INTERROGATIVE_RE.search(text)) or text.rstrip().endswith("?")


# Tool family sets keep the gate logic readable and DRY.
_PATH_A_TOOLS = {"patch_dry_run", "execute_patch_operation", "rollback_patches"}
_PATH_B_TOOLS = {"multi_account_dry_run", "multi_account_execute", "multi_account_rollback"}
# Install-only tools — used by rules that don't apply to rollback (CVE
# forwarding, severity_filter consistency, cross-env progression block).
_INSTALL_ONLY_TOOLS = {"execute_patch_operation", "multi_account_execute"}
# Rollback tools — destructive but with their own semantics. Tracked
# separately so they don't inherit
# install-specific rules (cve_id forwarding, severity matching, etc).
_ROLLBACK_TOOLS = {"rollback_patches", "multi_account_rollback"}
_PATH_A_TO_PATH_B = {
    "patch_dry_run": "multi_account_dry_run",
    "execute_patch_operation": "multi_account_execute",
    "rollback_patches": "multi_account_rollback",
}
_PATH_B_TO_PATH_A = {v: k for k, v in _PATH_A_TO_PATH_B.items()}


# ============================================================================
# PATCH MANAGER STEERING
# ============================================================================

class PatchWorkflowSteering(SteeringHandler):
    """Deterministic gates around the agent's patching workflow.

    Each rule fires just-in-time before a tool call. Guide() returns rich
    instructions to the LLM so the system prompt can stay terse — these
    handlers are where the verbose corrective guidance lives.

    Rules enforced:
    - Choose Path A (instance-ID tools) when the operator's current message names
      instance IDs. Path A targets EXACTLY those IDs.
    - Choose Path B (tag-based tools) when the operator's current message describes
      a fleet scope (env / severity / CVE) without naming instance IDs. Path B
      targets all instances matching tag:Environment + tag:PatchAutomation=enabled.
    - Run resolve_execution_scope before multi_account_execute so the operator sees
      a multi-account plan before approving.
    - Match severity_filter between dry-run and Install when both ran.
    - Forward operator-named CVE as cve_id= to keep the audit trail intact.
    - Block cross-environment progression after a failure until resolved.

    Write tools never execute: they return a stored plan that only the
    operator can approve, through the UI.
    """

    name = "patch_workflow"

    def __init__(self):
        super().__init__(context_providers=[LedgerProvider()])
        self._after_model_budget = _GuideBudget()

    async def steer_before_tool(self, *, agent, tool_use: ToolUse, **kwargs: Any) -> ToolSteeringAction:
        tool_name = tool_use["name"]
        completed = _completed_tool_names(self.steering_context)
        tool_input = tool_use.get("input", {}) or {}

        # Path A targets exactly the instance IDs passed to it, so it needs some.
        if tool_name in _PATH_A_TOOLS:
            if not tool_input.get("instance_ids"):
                return Guide(
                    reason=f"This request is fleet-scoped: the operator's message describes "
                           f"scope by environment, severity, or CVE. Use "
                           f"{_PATH_A_TO_PATH_B[tool_name]} to target all instances with the "
                           f"matching tags. Instance IDs from earlier turns or analyst replies "
                           f"are CVE impact data, not patch targets for this request."
                )

        # Choose Path B when the operator's current message describes a fleet
        # scope (env / severity / CVE) without naming instance IDs. Path B
        # targets all instances matching tag:Environment + tag:PatchAutomation=enabled.
        if tool_name in _PATH_B_TOOLS:
            user_text = _last_user_text(agent)
            if _INSTANCE_ID_RE.search(user_text):
                return Guide(
                    reason=f"This request is instance-targeted: the operator named instance "
                           f"ID(s) in their message. Use {_PATH_B_TO_PATH_A[tool_name]} with "
                           f"those exact instance_ids. Tag-based tools would target every "
                           f"instance with the matching tag, not just the named ones."
                )

        # Run resolve_execution_scope before multi_account_* so the operator
        # sees a multi-account plan (account fan-out, concurrency suggestions)
        # before approving. Path A tools target named instances directly and
        # don't need this step.
        if tool_name in ("multi_account_execute", "multi_account_dry_run") and "resolve_execution_scope" not in completed:
            return Guide(reason="Call resolve_execution_scope first to discover accounts.")

        # Severity filter consistency between dry-run and Install (Path A only —
        # multi_account_execute reads from the dry-run's execution_id directly).
        if tool_name == "execute_patch_operation":
            dry_run = _last_tool_call(self.steering_context, "patch_dry_run")
            if dry_run:
                dr_severity = dry_run.get("tool_args", {}).get("severity_filter")
                ex_severity = tool_input.get("severity_filter")
                if dr_severity and ex_severity and dr_severity != ex_severity:
                    return Guide(
                        reason=f"severity_filter mismatch: dry-run used '{dr_severity}', "
                               f"Install uses '{ex_severity}'. Use the same filter."
                    )

        # CVE-forwarding safety net: if the operator's current message names a
        # CVE, the install tool MUST receive cve_id= so the audit trail is
        # correct. Rollbacks don't take cve_id (they undo the last yum txn
        # regardless of CVE), so this rule is install-only.
        if tool_name in _INSTALL_ONLY_TOOLS and not (tool_input.get("cve_id") or "").strip():
            user_text = _last_user_text(agent)
            match = _CVE_RE.search(user_text)
            if match:
                cve = match.group(0).upper()
                return Guide(
                    reason=f"Operator named {cve} in the request but cve_id was not passed to "
                           f"{tool_name}. Retry with cve_id='{cve}' (and severity, cvss_score, "
                           "sla_hours, sla_source if known). Skip a kwarg only when the value is "
                           "genuinely unknown."
                )

        # Cross-environment progression within a conversation: guide the model
        # away from staging/prod after a write tool errored (raised) in a lower
        # environment and nothing has succeeded there since. The control that
        # holds across conversations is the staged promotion check on the plan
        # (helper/change_controls.py), enforced in code.
        # Same-env retries and Path A single-instance patches are unaffected.
        # Install-only — rollback after a failed install in a lower env should
        # not be blocked (operator may need to roll back THEN move on).
        if tool_name in _INSTALL_ONLY_TOOLS:
            current_env = tool_input.get("environment", "")
            ledger = _get_ledger(self.steering_context)
            env_order = {"dev": 0, "staging": 1, "prod": 2}
            # Track which environments have unresolved failures (no later success)
            resolved_envs: set = set()
            for call in reversed(ledger.get("tool_calls", [])):
                if call.get("tool_name") not in _INSTALL_ONLY_TOOLS:
                    continue
                call_env = call.get("tool_args", {}).get("environment", "")
                if not call_env:
                    continue
                if call.get("status") == "success":
                    resolved_envs.add(call_env)
                elif call.get("status") == "error" and call_env not in resolved_envs:
                    # Unresolved failure — block if progressing to higher env
                    if (current_env and call_env != current_env
                            and env_order.get(current_env, 0) > env_order.get(call_env, 0)):
                        return Guide(
                            reason=f"Previous execution in '{call_env}' failed and has not been "
                                   f"resolved. Resolve it before progressing to '{current_env}'. "
                                   f"Offer rollback or investigation."
                        )
                    break  # only check the most recent unresolved failure

        return Proceed(reason="Workflow check passed")

    async def steer_after_model(self, *, agent, message: Message,
                                stop_reason: StopReason, **kwargs: Any) -> ModelSteeringAction:
        """Force the model to invoke a tool when it renders text instead of acting.

        This handler covers:

        Path A invocation:
          1. Operator names instance IDs in their current message (clear Path A intent).
          2. LLM may run discovery (get_patch_compliance, etc.) and stop there,
             rendering tag-based-scoped fake confirmation messages instead of
             actually invoking execute_patch_operation. This handler detects
             "Path A intent + no execute_patch_operation call yet + text-only
             response" and asks the LLM to call the right tool.

        This requires a code-level rule because steer_before_tool never fires
        when the LLM doesn't make a tool call.
        """
        # Only intervene if the model produced text (no tool use) AND the
        # turn ended cleanly. Tool-use turns handle themselves.
        if stop_reason not in ("end_turn", "stop_sequence"):
            return Proceed(reason="Not an end-of-turn text response")
        # If the model already issued a tool use, nothing to do.
        for block in message.get("content", []) or []:
            if isinstance(block, dict) and ("toolUse" in block or "tool_use" in block):
                return Proceed(reason="Tool use already issued")

        ledger = _get_ledger(self.steering_context)
        user_text = _last_user_text(agent)

        # ── Path A intent + no execute_patch_operation call yet ──
        # Operator's current message names instance IDs AND the operator's
        # intent is patch/scan (verbs in the message). The LLM has not yet
        # invoked execute_patch_operation or patch_dry_run for this turn,
        # so it's about to render text instead of acting.
        if user_text and _INSTANCE_ID_RE.search(user_text) and not _is_question(user_text) \
                and not _READ_REQUEST_RE.match(user_text.strip()):
            # "patch run 0b3965e9" names a run; it isn't a request to patch (disruption round, 2 Nov).
            action_text = _PATCH_NOUN_RE.sub(" ", user_text)
            verb = re.search(r'\b(patch|fix|remediate|apply|execute|install|scan|dry[\s-]?run|preview)\b',
                             action_text, re.IGNORECASE)
            if verb:
                wanted_tool = "patch_dry_run" if verb.group(0).lower() in {"scan", "dry-run", "dryrun", "dry run", "preview"} \
                    else "execute_patch_operation"
                # Find the most recent call to wanted_tool
                last_attempt = None
                for call in reversed(ledger.get("tool_calls", [])):
                    if call.get("tool_name") == wanted_tool:
                        last_attempt = call
                        break
                if last_attempt is None and self._after_model_budget.spend(agent):
                    return Guide(
                        reason=f"The operator's message names instance ID(s) and asks to "
                               f"{verb.group(0).lower()}. Do NOT respond with text or describe "
                               f"a plan yourself. Call {wanted_tool} with "
                               "instance_ids=[the IDs the operator named] and the relevant "
                               "kwargs (cve_id, severity, environment, etc.). It returns a "
                               "plan card scoped to those instances for the operator to "
                               "approve. Do NOT call get_patch_compliance, "
                               "resolve_execution_scope, or any tag-based discovery tool; "
                               "Path A targets the named instances directly."
                    )

        return Proceed(reason="No pending action requires correction")


# ============================================================================
# COMPLIANCE OUTPUT STEERING
# ============================================================================

class ComplianceOutputSteering(SteeringHandler):
    """Prevent dangerous misrepresentation in compliance output.

    Rules enforced:
    1. Never claim "100% compliance" when total_reports = 0
    """

    name = "compliance_output"

    def __init__(self):
        super().__init__(context_providers=[LedgerProvider()])
        self._after_model_budget = _GuideBudget()

    async def steer_after_model(self, *, agent, message: Message,
                                stop_reason: StopReason, **kwargs: Any) -> ModelSteeringAction:
        # Extract text from message content
        text = ""
        for block in message.get("content", []):
            if isinstance(block, dict) and "text" in block:
                text += block["text"]
            elif isinstance(block, str):
                text += block

        if not text:
            return Proceed(reason="No text in response")

        # Check for zero-data compliance claims
        last_query = _last_tool_call(self.steering_context, "query_compliance_reports")
        if last_query:
            result = last_query.get("result", [])
            # Result is a list of content blocks; check if total_count is 0
            result_str = str(result)
            has_zero_reports = "'total_count': 0" in result_str or '"total_count": 0' in result_str

            if has_zero_reports and "100%" in text and self._after_model_budget.spend(agent):
                return Guide(
                    reason="Do not claim '100% compliance' when total_reports is 0. "
                           "Zero reports means no patching activity was recorded, not compliance. "
                           "Say 'No compliance reports found for this period' instead."
                )

        return Proceed(reason="Output check passed")
