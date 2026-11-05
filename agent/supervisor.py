"""Patch Automation Agent — AgentCore entrypoint."""

import asyncio
import json
import logging
import os
import sys
import time
from types import SimpleNamespace
from typing import AsyncIterator

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import RequestContext
from helper import caller
from helper.agent_factory import create_agent
from helper.cards import CardCollector
from helper.stream_filter import client_event, usage_event
from helper.tools import (
    # Vulnerability Analysis
    get_vulnerability_findings, assess_fleet_impact,
    # Patch Operations
    get_patch_compliance, patch_dry_run, execute_patch_operation,
    get_command_status, rollback_patches, verify_rollback,
    multi_account_dry_run, multi_account_execute, multi_account_rollback,
    get_automation_status,
    # Compliance & Verification
    capture_patch_state, verify_cve_remediation, query_compliance_reports, forecast_sla_misses,
    # Fleet & Infrastructure
    get_fleet_overview, resolve_execution_scope,
    # Maintenance & Health
    get_maintenance_windows, create_maintenance_window, get_patch_policy,
    check_instance_health, check_cloudwatch_alarms, verify_and_proceed,
    # Shared utilities
    set_operator, set_timezone, clear_request_scans,
)
from helper.tools._shared import set_operator_message, set_session_id
from helper.tools.plan_tools import run_approved_plan, run_card, run_note, stop_card, stop_run

# Configure logging — use stdout so OTEL auto-instrumentation captures logs.
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
logger = logging.getLogger(__name__)

# DEBUG on these logs full prompts, tool inputs and outputs, and memory
# contents to CloudWatch; set PATCHY_DEBUG_LOGS=true only while diagnosing.
_sdk_level = logging.DEBUG if os.environ.get("PATCHY_DEBUG_LOGS", "").lower() == "true" else logging.INFO
logging.getLogger("strands").setLevel(_sdk_level)
logging.getLogger("bedrock_agentcore.memory").setLevel(_sdk_level)

# Configure environment
os.environ["BYPASS_TOOL_CONSENT"] = os.environ.get("BYPASS_TOOL_CONSENT", "true")
os.environ["STRANDS_TOOL_CONSOLE_MODE"] = "disabled"

# Initialize AgentCore app
app = BedrockAgentCoreApp()

# Largest turn measured in Release B used about 1,400 output tokens including thinking.
# Bedrock reserves max_tokens against the tokens-per-minute quota on every call.
MAX_TOKENS = 8000

SYSTEM_PROMPT = """You are a Patch Automation Agent for EC2 infrastructure.
Wrap final answers in <answer></answer> tags.
When a tool call is cancelled with guidance, follow the instructions and retry.
Out of scope → <answer>That's outside what this system handles.</answer>

RULES:
- "patch"/"fix"/"remediate"/"roll back" → call the write tool. It returns a plan the operator approves on a card. You never execute changes and can't approve them; if the operator types approval, point them to the Approve button.
- "stop"/"abort"/"cancel" a run → you can't stop runs. Tell the operator to press Stop on the run's card, and offer its status.
- Do NOT run a scan/dry-run before a PATCH request unless the operator explicitly asks.
- Instance IDs from earlier turns are CONTEXT, not targets. If ambiguous, ask.
- Do NOT pass account_id unless operator explicitly names one.
- A plan card warns when the previous stage (dev before staging, staging before prod) wasn't verified; say so in one line.
- assess_fleet_impact: call for top 1-3 CVEs by CVSS only, not every CVE.
- Zero compliance reports = "No patching activity recorded", NOT "100% compliance."
- sla_met=null → "SLA outcome not recorded", not a breach.
- If operator's intent is ambiguous between multiple actions, ask ONE clarifying question. Never more than one.
- Why a plan runs now: repeat its why_now or window_check text. Never use the word "emergency" for a plan or run; decision EMERGENCY in a plan or report means it ran when approved, not in a window ("patched now").
- A result with error_code was refused: say what was refused and the tool's reason in one or two sentences, then its next_action. Don't retry with other parameters to get around it.
- Describe a run in the tool's own words (cancelled, finishing, couldn't be read). A cancelled run didn't fail, and a finishing one isn't done.

OUTPUT:
- A tool result marked shown_as_card is already on screen as a card: write two or three sentences on what matters and what to do. No table, and don't repeat its fields, IDs or expiry. The rules below are for results without a card.
- Lead with a one-line answer. Count question → that line only. List → table, max 10 rows, then summarise the rest. **Bold** key numbers and decisions. > blockquote for SLA-critical warnings.
- Vulnerabilities: a severity count line, then | CVE | CVSS | Severity | Instances | Environments | (top 10 by CVSS). One CVE's impact: | Environment | Instances | SLA deadline | Next window |, then the rollout order.
- Status: overall result and counts; a table only for failures or mixed results.
- A started scan: what started, its execution ID exactly as returned, and that it runs in the background so the operator can ask for status later. Never say anything started without an execution ID in a tool result.
- Nothing found → one line, no speculation about why.
- Time: relative first ("19 hours"), absolute in parentheses. Full 12-digit account IDs. Name (i-xxx) when known.
- Later turns shorter; never re-explain what the operator already saw. No emojis, no em dashes, no fabricated data.
- End with ## Next Steps: up to 3 requests under 80 chars the operator could send, e.g. "Preview patches for dev". Leave it out under a plan card; the card has the buttons."""


ALL_TOOLS = [
    # Vulnerability Analysis
    get_vulnerability_findings, assess_fleet_impact,
    # Patch Operations
    get_patch_compliance, patch_dry_run, execute_patch_operation,
    get_command_status, multi_account_dry_run, multi_account_execute,
    get_automation_status,
    # Rollback
    rollback_patches, verify_rollback, multi_account_rollback,
    # Fleet & Infrastructure
    get_fleet_overview, resolve_execution_scope,
    get_maintenance_windows, get_patch_policy, create_maintenance_window,
    # Compliance & Verification
    capture_patch_state, verify_cve_remediation, query_compliance_reports, forecast_sla_misses,
    # Health Checks
    check_instance_health, check_cloudwatch_alarms, verify_and_proceed,
]


@app.entrypoint
async def agent_invocation(payload: dict, context: RequestContext) -> AsyncIterator[dict]:
    """Handler for agent invocation: a chat turn, or a run_plan or stop_run action."""
    user_message = payload.get("prompt", "")
    operator_identity = payload.get("operator", "unknown")
    user_timezone = payload.get("timezone", "UTC")
    caller.set_request(context)
    # In JWT mode the validated token says who is calling, not the payload, and
    # a conversation belongs to the first user who used it.
    if caller.jwt_mode():
        # The session is bound to the username; records show the email from the user pool.
        operator_identity = caller.display_name(context) or "unknown"
        if not caller.claim_session(context.session_id, caller.username(context) or "unknown"):
            yield {"event": {"contentBlockDelta": {"delta": {
                "text": "This conversation belongs to another user. Start a new conversation."}}}}
            return

    # Set request-scoped context
    set_operator(operator_identity)
    set_timezone(user_timezone)
    set_session_id(context.session_id)
    set_operator_message(user_message)
    clear_request_scans()

    try:
        memory_id = os.environ.get("MEMORY_PATCHMEMORYV2_ID")
        session_id = context.session_id

        # An approved plan runs deterministically from its stored parameters;
        # the model only narrates the outcome afterwards.
        # Stop is a safety action: it runs without the model and ends the turn.
        # With RUNTIME_AUTH=jwt the caller's own token, not the payload, decides
        # whether they may run or stop a change.
        if payload.get("action") in ("stop_run", "run_plan") and not caller.may_change(context):
            logger.warning(f"[AUTH] {payload['action']} refused: {caller.username(context)} is not an operator")
            refusal = {"error": "Only operators can run or stop changes."}
            if payload["action"] == "stop_run":
                yield {"card": stop_card(str(payload.get("execution_id", "")), refusal)}
            else:
                yield {"card": run_card(str(payload.get("plan_id", "")), refusal)}
            return

        if not payload.get("action") and not caller.may_chat(context):
            logger.warning(f"[AUTH] chat refused: {caller.username(context)} is not an operator")
            yield {"event": {"contentBlockDelta": {"delta": {
                "text": "Only operators can use the assistant."}}}}
            return

        if payload.get("action") == "stop_run":
            execution_id = str(payload.get("execution_id", ""))
            # AWS calls in a worker thread (contextvars go with it), not on the shared loop.
            stopped = await asyncio.to_thread(stop_run, execution_id, session_id)
            yield {"card": stop_card(execution_id, stopped)}
            return

        if payload.get("action") == "run_plan":
            plan_id = str(payload.get("plan_id", ""))
            ran = await asyncio.to_thread(run_approved_plan, plan_id, session_id)
            yield {"card": run_card(plan_id, ran)}
            user_message = run_note(plan_id, ran)

        logger.info(f"[AGENT] Request: operator={operator_identity} session={session_id}")
        logger.info(f"[AGENT] Message: {user_message[:80]}")

        agent_context = SimpleNamespace(
            memory_id=memory_id,
            session_id=session_id,
            operator_id=operator_identity,
        )

        cards = CardCollector()
        agent = create_agent(
            name="patch-automation-unified",
            system_prompt=SYSTEM_PROMPT,
            tools=ALL_TOOLS,
            context=agent_context,
            max_tokens=MAX_TOKENS,
            hooks=[cards],
        )

        # Stream response
        start_time = time.time()
        result = None
        first_text_ms = None
        stream = agent.stream_async(user_message)
        async for event in stream:
            if isinstance(event, dict) and "result" in event:
                result = event["result"]
            # Raw Strands events carry the system prompt, tool specs and agent
            # state. Send the caller only tool names and answer text.
            safe = client_event(event)
            if safe is not None:
                if first_text_ms is None and "contentBlockDelta" in safe["event"]:
                    first_text_ms = int((time.time() - start_time) * 1000)
                yield safe
            # Display blocks stripped from tool results, sent as result cards
            for card in cards.drain():
                yield {"card": card}

        elapsed_ms = int((time.time() - start_time) * 1000)
        usage = usage_event(result, elapsed_ms, first_text_ms)
        logger.info(f"[USAGE] {json.dumps(usage['usage'])}")
        yield usage
    finally:
        clear_request_scans()


if __name__ == "__main__":
    app.run()
