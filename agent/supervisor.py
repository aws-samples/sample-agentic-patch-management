"""Patch Automation Agent — AgentCore entrypoint."""

import os
import sys
import time
import logging
from types import SimpleNamespace

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from helper.agent_factory import create_agent
from helper.tools import (
    # Vulnerability Analysis
    get_vulnerability_findings, assess_fleet_impact,
    # Patch Operations
    get_patch_compliance, patch_dry_run, execute_patch_operation,
    get_command_status, rollback_patches, verify_rollback,
    multi_account_dry_run, multi_account_execute, multi_account_rollback,
    get_automation_status, emergency_stop,
    # Compliance & Verification
    capture_patch_state, verify_cve_remediation, query_compliance_reports,
    # Fleet & Infrastructure
    get_fleet_overview, resolve_execution_scope,
    # Maintenance & Health
    get_maintenance_windows, create_maintenance_window, get_patch_policy,
    check_instance_health, check_cloudwatch_alarms, verify_and_proceed,
    # Shared utilities
    set_operator, set_timezone, clear_request_scans,
)
from helper.tools._shared import _get_fleet_summary

# Configure logging — use stdout so OTEL auto-instrumentation captures logs.
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
logger = logging.getLogger(__name__)

logging.getLogger("strands").setLevel(logging.DEBUG)
logging.getLogger("bedrock_agentcore.memory").setLevel(logging.DEBUG)

# Configure environment
os.environ["BYPASS_TOOL_CONSENT"] = os.environ.get("BYPASS_TOOL_CONSENT", "true")
os.environ["STRANDS_TOOL_CONSOLE_MODE"] = "disabled"

# Initialize AgentCore app
app = BedrockAgentCoreApp()

SYSTEM_PROMPT = """You are a Patch Automation Agent for EC2 infrastructure.
Wrap final answers in <answer></answer> tags.
When a tool call is cancelled with guidance, follow the instructions and retry.
Out of scope → <answer>That's outside what this system handles.</answer>

RULES:
- "patch"/"fix"/"remediate" → go directly to the execute tool. Its confirmation_required response IS the preview.
- Confirmation pattern: omit confirm_execute on first call → tool returns plan → operator approves → call SAME tool with confirm_execute=True (same parameters).
- Do NOT run a scan/dry-run before a PATCH request unless the operator explicitly asks.
- Instance IDs from earlier turns are CONTEXT, not targets. If ambiguous, ask.
- Do NOT pass account_id unless operator explicitly names one.
- Prod patching without prior dev/staging success: append one advisory line.
- assess_fleet_impact: call for top 1-3 CVEs by CVSS only, not every CVE.
- Zero compliance reports = "No patching activity recorded", NOT "100% compliance."
- sla_met=null → "SLA outcome not recorded", not a breach.
- If operator's intent is ambiguous between multiple actions, ask ONE clarifying question. Never more than one.

OUTPUT:
- get_response_template before final answers. Template labels are meta-instructions, never output them.
- Depth match: count question → one line. list → table. plan → structured detail.
- Later turns = shorter. Confirmations: 3-5 lines. Status: summary + table only if mixed. Never re-explain what operator already saw.
- Tables: max 5 rows, summarize overflow. Show Name (i-xxx) when available. Always show full 12-digit account IDs.
- Time: relative first ("19 hours"), absolute in parentheses. Never bare UTC without relative.
- > blockquote for SLA-critical warnings. **bold** key numbers and decisions.
- End ## Next Steps (3 actions <80 chars). No emojis. No fabricated data. Surface execution IDs."""


ALL_TOOLS = [
    # Vulnerability Analysis
    get_vulnerability_findings, assess_fleet_impact,
    # Patch Operations
    get_patch_compliance, patch_dry_run, execute_patch_operation,
    get_command_status, multi_account_dry_run, multi_account_execute,
    get_automation_status, emergency_stop,
    # Rollback
    rollback_patches, verify_rollback, multi_account_rollback,
    # Fleet & Infrastructure
    get_fleet_overview, resolve_execution_scope,
    get_maintenance_windows, get_patch_policy, create_maintenance_window,
    # Compliance & Verification
    capture_patch_state, verify_cve_remediation, query_compliance_reports,
    # Health Checks
    check_instance_health, check_cloudwatch_alarms, verify_and_proceed,
]


def _get_fleet_context_line() -> str:
    """Build a brief fleet state line from cached data (~30 tokens)."""
    try:
        fleet = _get_fleet_summary()
        if not fleet or not fleet.get('totals'):
            return ""
        totals = fleet['totals']
        envs = fleet.get('environments', {})
        env_counts = ", ".join(
            f"{len(v.get('instances', []))} {k}"
            for k, v in envs.items() if v.get('instances')
        )
        managed = totals.get('managed_count', 0)
        missing = totals.get('total_missing', 0)
        return f"{managed} instances ({env_counts}) | {missing} missing patches"
    except Exception:
        return ""


@app.entrypoint
async def agent_invocation(payload, context):
    """Handler for agent invocation."""
    user_message = payload.get("prompt", "")
    operator_identity = payload.get("operator", "unknown")
    user_timezone = payload.get("timezone", "UTC")

    # Set request-scoped context
    set_operator(operator_identity)
    set_timezone(user_timezone)
    clear_request_scans()

    try:
        memory_id = os.environ.get("MEMORY_PATCHMEMORYV2_ID")
        session_id = context.session_id

        logger.info(f"[AGENT] Request: operator={operator_identity} session={session_id}")
        logger.info(f"[AGENT] Message: {user_message[:80]}")

        # Pre-computed fleet context (reduces discovery tool calls)
        fleet_context = _get_fleet_context_line()
        if fleet_context:
            enriched_message = f"[FLEET STATE] {fleet_context}\n\n{user_message}"
        else:
            enriched_message = user_message

        agent_context = SimpleNamespace(
            memory_id=memory_id,
            session_id=session_id,
            operator_id=operator_identity,
        )

        agent = create_agent(
            name="patch-automation-unified",
            system_prompt=SYSTEM_PROMPT,
            tools=ALL_TOOLS,
            context=agent_context,
            max_tokens=16000,
        )

        # Stream response
        start_time = time.time()
        result = None
        stream = agent.stream_async(enriched_message)
        async for event in stream:
            if hasattr(event, 'result'):
                result = event.result
            yield event

        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.info(f"[AGENT] Completed in {elapsed_ms}ms")
    finally:
        clear_request_scans()


if __name__ == "__main__":
    app.run()
