"""Supervisor Agent with orchestration capabilities"""

import os
import time
import logging
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import tool
from strands_tools import use_aws
from vulnerability_agent import gather_vulnerabilities
from patch_manager import patching_operations
from compliance_analyst import analyze_compliance

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configure environment
os.environ["BYPASS_TOOL_CONSENT"] = os.environ.get("BYPASS_TOOL_CONSENT", "true")
os.environ["STRANDS_TOOL_CONSOLE_MODE"] = "disabled"

# Initialize AgentCore app
app = BedrockAgentCoreApp()

SYSTEM_PROMPT = """You are a Patch Automation Coordinator. Route queries to specialists and present their responses.

Wrap final answers in <answer></answer> tags.

ROUTING:
1. Identify primary intent from current query
2. Use conversation history for context (but current query overrides if conflicting)
3. Route by intent:

consult_vulnerability_analyst → CVE analysis, CVSS scores, affected instances, fleet impact, impact assessment, "what vulnerabilities", "show vulns", "how widespread"
consult_patch_manager → Patching, patch compliance (SSM patch states, missing patches), maintenance windows, SLA, health checks, rollback, rollback verification, dry-run, preview patches, "proceed/continue", generate compliance report, capture patch state
consult_compliance_analyst → Compliance REPORTS (S3 audit data), SLA breach history, audit trails, trends, "show reports"
use_aws → Direct AWS queries NOT about vulnerabilities/patching/compliance

Key distinction: "patch compliance" (missing patches, installed count) → patch_manager. "compliance reports" (audit history, SLA trends) → compliance_analyst. "generate compliance report" → patch_manager. "dry run" / "preview" / "what will change" → patch_manager. "verify rollback" → patch_manager.

If query spans domains (e.g., "find vulns and patch them"), call vulnerability analyst first, then patch manager with the results.

CONTEXT FORWARDING: When routing follow-up queries ("proceed", "check status", "verify"):
- Include relevant IDs from conversation history in the query to the specialist
- Example: user says "check status" → route to patch manager with "Check status of command {cmd_id} on instances {ids} in {env}"
- Never forward a bare "proceed" — always enrich with the context the specialist needs.
- You have full conversation history. The specialist does NOT. Extract and include: command_ids, instance_ids, CVE IDs, environments, window_ids — anything the specialist needs to act without asking the user.
- CRITICAL for vuln→patch handoff: When vulnerability analyst returns affected_resources with instance IDs, ALWAYS include those instance IDs in the query to patch_manager. Example: "Build remediation plan for CVE-2024-1234 affecting instances i-abc, i-def in dev, staging, prod"

OUT OF SCOPE — handle directly, do NOT route to any specialist:
- Policy rationale ("why is the SLA X hours?", "who decided the patching window?")
- Organizational questions ("who owns security policy?", "what team manages compliance?")
- General knowledge ("what is PCI-DSS?", "explain CVE scoring")
- Anything not about discovering, patching, or reporting on vulnerabilities in THIS fleet
→ Reply: "That's outside what this system handles. I can help with vulnerability scanning, patch management, and compliance reporting for your fleet." Keep it to 1-2 sentences. Never fabricate context, teams, processes, or recommendations.

RULES:
- Trust specialist responses — do not reinterpret or fabricate data
- If query is ambiguous (e.g., "patch test"), ask: which environment? which CVE?
- RETRYABLE errors: retry specialist once. ABORT errors: report to user immediately.
- Batch use_aws calls when possible (filters > loops)
- NEVER fabricate data. If a specialist returns OUT_OF_SCOPE, relay it concisely — do not add explanation, context, or suggestions that didn't come from a tool response.

OUTPUT:
- Lead with status emoji (✅ ⚠️ ❌) and 1-line summary
- Tables for >3 items (max 3 columns)
- Target <500 words. No conditional language.
- Use response templates for structure — do not invent new formats

NEXT STEPS (required at end of every response):
Write "## Next Steps" with exactly 3 numbered actions the operator can click to execute.

Rules:
1. Reference specific data from your response (CVE IDs, counts, environments, CVSS scores)
2. Plain language — write what the operator wants to do, not system jargon
3. Under 80 characters each. Never repeat the same action type.
4. ONLY reference environments where data exists in the response

Action patterns (adapt phrasing naturally):
- PLAN: "Build remediation plan for [N] [severity] CVEs in [env]"
- PREVIEW: "Preview patches for [env] — [N] instances, [N] CVEs"
- PATCH: "Start patching [env] — [N] instances ([severity] CVEs)"
- STAGED ROLLOUT: "Stage rollout: [env1] → [env2] ([N] CVEs)"
- VERIFY: "Check patch results for [env]"
- AUDIT: "Generate compliance report for [env]"
- ROLLBACK: "Roll back [env] patches"
- VERIFY ROLLBACK: "Confirm rollback succeeded in [env]"
- SCAN: "Check if [env] is also affected"

After vulnerability scan → 1. Preview patches for the most affected environment, 2. Build remediation plan across affected environments, 3. Check if unscanned environments are affected
After patching → 1. Check patch results, 2. Generate compliance report, 3. Start next environment or next severity
After rollback → 1. Confirm rollback succeeded, 2. Investigate root cause, 3. Re-plan patching approach
After compliance report → 1. Address SLA breaches, 2. Patch non-compliant instances, 3. Review audit trail"""


def _route_to_specialist(specialist_name: str, agent_factory_fn, query: str,
                         keywords: list[str], memory_id: str | None, session_id: str,
                         operator_id: str = "unknown") -> str:
    """Shared routing logic for all specialist consultations.
    
    Enriches the query with conversation context, creates a stateless specialist
    agent, invokes it, and returns the response text.
    """
    from helper.decision_logger import get_decision_logger
    from types import SimpleNamespace

    decision_logger = get_decision_logger("supervisor")
    decision_logger.log_routing_decision(
        user_query=query,
        specialist=specialist_name,
        keywords_matched=keywords,
        confidence="high"
    )

    # Specialists get read-only memory access — they can see the supervisor's
    # conversation history (including tool results from other specialists) but
    # don't write to it, keeping the supervisor's memory clean.
    enhanced_query = query
    context = SimpleNamespace(memory_id=memory_id, session_id=session_id,
                              operator_id=operator_id, memory_read_only=True)
    agent = agent_factory_fn(context)
    result = agent(enhanced_query)
    return result.message['content'][0]['text']


def _make_tools(memory_id: str | None, session_id: str, operator_id: str = "unknown"):
    """Create tool functions with captured context (no globals needed)."""

    @tool
    def consult_vulnerability_analyst(query: str) -> str:
        """Get vulnerability analysis from security specialist."""
        return _route_to_specialist(
            "vulnerability_analyst", gather_vulnerabilities, query,
            ["CVE", "vulnerability", "security"], memory_id, session_id, operator_id
        )

    @tool
    def consult_patch_manager(query: str) -> str:
        """Get patch deployment plan from infrastructure specialist."""
        return _route_to_specialist(
            "patch_manager", patching_operations, query,
            ["patch", "instance", "deploy"], memory_id, session_id, operator_id
        )

    @tool
    def consult_compliance_analyst(query: str) -> str:
        """Get compliance reporting and audit trail analysis."""
        return _route_to_specialist(
            "compliance_analyst", analyze_compliance, query,
            ["compliance", "report", "audit"], memory_id, session_id, operator_id
        )

    return [consult_vulnerability_analyst, consult_patch_manager, consult_compliance_analyst]


@app.entrypoint
async def agent_invocation(payload, context):
    """Handler for agent invocation with AgentCore memory integration."""
    from helper.agent_factory import create_agent
    
    request_start = time.time()
    
    user_message = payload.get("prompt", "Hello! I'm ready to help with intelligent patch automation.")
    operator_identity = payload.get("operator", "unknown")
    
    # Set operator identity in request-scoped context (safe for concurrent requests)
    from helper.tools import set_operator
    set_operator(operator_identity)
    
    logger.info(f"New request: operator={operator_identity} message={user_message[:100]}")
    
    # Load memory_id from AgentCore config (set during agentcore launch)
    memory_id = os.environ.get("AGENTCORE_MEMORY_ID")
    if not memory_id:
        try:
            import yaml
            config_path = os.path.join(os.path.dirname(__file__), ".bedrock_agentcore.yaml")
            with open(config_path) as f:
                ac_config = yaml.safe_load(f)
            default_agent = ac_config.get("default_agent", "")
            memory_id = ac_config.get("agents", {}).get(default_agent, {}).get("memory", {}).get("memory_id")
        except Exception as e:
            logger.warning(f"Failed to load memory_id from config: {e}")
    
    if not memory_id:
        logger.warning("No memory_id found — memory will not persist across sessions")
    
    logger.info(f"✓ Memory enabled: {memory_id}")
    logger.info(f"Session ID: {context.session_id}")
    
    # Create context with memory_id and operator identity
    from types import SimpleNamespace
    agent_context = SimpleNamespace(
        memory_id=memory_id,
        session_id=context.session_id,
        operator_id=operator_identity
    )
    
    # Build tools with captured context (no globals)
    specialist_tools = _make_tools(memory_id, context.session_id, operator_identity)
    
    # Create agent with factory
    agent = create_agent(
        name="patch-automation-supervisor",
        system_prompt=SYSTEM_PROMPT,
        tools=specialist_tools + [use_aws],
        context=agent_context
    )
    
    # Stream response
    stream = agent.stream_async(user_message)
    async for event in stream:
        yield event
    
    # Log completion
    total_duration_ms = (time.time() - request_start) * 1000
    logger.info(f"Request completed in {total_duration_ms:.0f}ms")

if __name__ == "__main__":
    app.run()
