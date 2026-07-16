"""Confirmation goal validator — framework-level confirmation state machine.

Replaces the after-model State 1 logic from PatchWorkflowSteering with a
dedicated handler that only concerns itself with the confirmation retry pattern.
This makes the confirmation flow independent of other workflow steering rules.

The pattern it enforces:
  1. Tool returns status=confirmation_required with a plan.
  2. LLM presents the plan to the operator.
  3. Operator approves ("yes", "proceed", etc.).
  4. LLM MUST call the same tool again with confirm_execute=True.

Without this handler, the LLM sometimes fails to retry (talks instead of acting).
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
)
from strands.types.content import Message
from strands.types.streaming import StopReason

logger = logging.getLogger(__name__)

# Words that count as operator approval. Matched at start of message.
_AFFIRMATIVE_RE = re.compile(
    r'^\s*(yes|y|yeah|yep|yup|sure|ok|okay|proceed|confirm|confirmed|do it|go ahead|approve|approved|continue)\b',
    re.IGNORECASE,
)

# Tools that use the confirmation pattern (confirm_execute gate).
_CONFIRMABLE_TOOLS = {
    "execute_patch_operation",
    "multi_account_execute",
    "rollback_patches",
    "multi_account_rollback",
}


def _is_affirmative(text: str) -> bool:
    """Heuristic: did the operator just say 'yes' (or close enough)?"""
    return bool(_AFFIRMATIVE_RE.search(text.strip())) if text else False


def _last_user_text(agent) -> str:
    """Extract the text of the most recent operator (user) message."""
    try:
        messages = getattr(agent, "messages", None) or []
    except Exception:
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content") or []
        for block in (content if isinstance(content, list) else [content]):
            text = block.get("text") if isinstance(block, dict) else str(block)
            if text:
                return text
        return ""
    return ""


class ConfirmationGoalHandler(SteeringHandler):
    """Ensure pending confirmations are properly retried after operator approval.

    This handler only fires after model responses (text-only, no tool use).
    It checks whether a tool previously returned confirmation_required AND the
    operator's latest message is affirmative — if so, it instructs the model
    to call the tool with confirm_execute=True.
    """

    name = "confirmation_goal"

    def __init__(self):
        super().__init__(context_providers=[LedgerProvider()])

    async def steer_after_model(self, *, agent, message: Message,
                                stop_reason: StopReason, **kwargs: Any) -> ModelSteeringAction:
        # Only intervene on end-of-turn text responses (no tool use).
        if stop_reason not in ("end_turn", "stop_sequence"):
            return Proceed(reason="Not an end-of-turn text response")

        for block in message.get("content", []) or []:
            if isinstance(block, dict) and ("toolUse" in block or "tool_use" in block):
                return Proceed(reason="Tool use already issued")

        ledger = self._get_ledger()
        user_text = _last_user_text(agent)

        # Find the most recent confirmable tool call
        pending_tool = None
        pending_gate = None
        for call in reversed(ledger.get("tool_calls", [])):
            name = call.get("tool_name")
            if name not in _CONFIRMABLE_TOOLS:
                continue
            result = call.get("result", {})
            if self._is_confirmation_required(result):
                pending_tool = name
                result_str = str(result)
                if "NoScanConfirmation" in result_str:
                    pending_gate = "no_scan"
                else:
                    pending_gate = "execute"
            break

        # Fallback: any tool returning confirmation_required is confirmable
        # (catches new tools with confirmation patterns not in _CONFIRMABLE_TOOLS)
        if not pending_tool:
            for call in reversed(ledger.get("tool_calls", [])):
                result = call.get("result", {})
                if self._is_confirmation_required(result):
                    pending_tool = call.get("tool_name")
                    result_str = str(result)
                    if "NoScanConfirmation" in result_str:
                        pending_gate = "no_scan"
                    else:
                        pending_gate = "execute"
                    break

        if not pending_tool or not _is_affirmative(user_text):
            return Proceed(reason="No pending confirmation or operator hasn't approved")

        # Build retry guidance
        if pending_gate == "no_scan":
            kwargs_hint = ("with confirm_execute=True AND confirm_no_scan=True "
                           "(plus the same other parameters as the prior call)")
        else:
            kwargs_hint = ("with confirm_execute=True (and the same other "
                           "parameters as the prior call). Do NOT also set "
                           "confirm_no_scan on this retry — if there is a "
                           "separate no-scan advisory, the tool will return it "
                           "as another confirmation_required and the operator "
                           "will approve it next")

        return Guide(
            reason=f"The operator approved the plan ('{user_text.strip()[:40]}'). "
                   f"Do NOT respond with text. Call {pending_tool} again immediately "
                   f"{kwargs_hint}. Reporting another confirmation message yourself "
                   "is not allowed at this stage — the gate already accepted the "
                   "operator's approval."
        )

    @staticmethod
    def _is_confirmation_required(result) -> bool:
        """Check if a tool result indicates confirmation is required.

        Handles both dict results (native) and string representations.
        """
        if isinstance(result, dict):
            return result.get('status') == 'confirmation_required'
        # Fallback: string matching for stringified dicts
        result_str = str(result)
        return ("'status': 'confirmation_required'" in result_str
                or '"status": "confirmation_required"' in result_str)

    def _get_ledger(self) -> dict:
        """Get the tool call ledger from steering context."""
        context_data = self.steering_context.data.get() or {}
        return context_data.get("ledger", {})
