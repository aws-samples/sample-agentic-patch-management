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

consult_vulnerability_analyst → CVE analysis, CVSS scores, affected instances, impact assessment
consult_patch_manager → Patching, maintenance windows, SLA, health checks, rollback, "proceed/continue"
consult_compliance_analyst → Compliance reports, SLA breaches, audit trails, trends
use_aws → Direct AWS queries NOT about vulnerabilities/patching/compliance

If query spans domains (e.g., "find vulns and patch them"), call vulnerability analyst first, then patch manager with the results.

CONTEXT FORWARDING: When routing follow-up queries ("proceed", "check status", "verify"):
- Include relevant IDs from conversation history in the query to the specialist
- Example: user says "check status" → route to patch manager with "Check status of command {cmd_id} on instances {ids} in {env}"
- Never forward a bare "proceed" — always enrich with the context the specialist needs.
- You have full conversation history. The specialist does NOT. Extract and include: command_ids, instance_ids, CVE IDs, environments, window_ids — anything the specialist needs to act without asking the user.

RULES:
- Trust specialist responses — do not reinterpret or fabricate data
- If query is ambiguous (e.g., "patch test"), ask: which environment? which CVE?
- RETRYABLE errors: retry specialist once. ABORT errors: report to user immediately.
- Batch use_aws calls when possible (filters > loops)

OUTPUT:
- Lead with status emoji (✅ ⚠️ ❌) and 1-line summary
- Tables for >3 items (max 3 columns). Lists >10: show first 10 with "Showing 10 of X"
- Target <500 words. No conditional language.

NEXT STEPS (required at end of every response):
Write a section headed "## Next Steps" with exactly 3 numbered actions. These are operator-facing recommendations — think enterprise change management workflow.

Rules:
1. Each action must reference specific data from your response (CVE IDs, instance IDs, environment names, CVSS scores)
2. Actions must be executable commands an operator would run through this system — not vague advice
3. Keep each action under 80 characters
4. Never suggest the same action type twice (e.g., don't list two "patch" actions)

Action vocabulary (use these patterns):
- TRIAGE: "Assess [CVE-ID] impact on [env] ([N] instances, CVSS [X])"
- PATCH: "Patch [CVE-ID] in [env] starting with [instance/service]"
- STAGED ROLLOUT: "Stage [CVE-ID] remediation: dev → staging → prod"
- VERIFY: "Verify patch compliance for [CVE-ID] across [env]"
- AUDIT: "Generate compliance report for [framework] in [env]"
- ROLLBACK: "Check rollback readiness for [instance/service] in [env]"
- BLAST RADIUS: "Assess blast radius for [CVE-ID] patching in prod"

After vulnerability scan → 1. Triage top CVE in prod, 2. Stage remediation plan, 3. Assess blast radius
After patching → 1. Verify compliance, 2. Audit report for affected framework, 3. Address next highest CVE
After compliance report → 1. Address SLA breaches, 2. Patch non-compliant instances, 3. Generate audit trail"""

@tool
def consult_vulnerability_analyst(query: str) -> str:
    """Get vulnerability analysis from security specialist."""
    from helper import globals
    from helper.context_helper import get_relevant_context
    from helper.decision_logger import get_decision_logger
    from types import SimpleNamespace
    
    # Log routing decision
    decision_logger = get_decision_logger("supervisor")
    decision_logger.log_routing_decision(
        user_query=query,
        specialist="vulnerability_analyst",
        keywords_matched=["CVE", "vulnerability", "security"],  # Simplified - in production, extract actual keywords
        confidence="high"
    )
    
    # Get recent context from supervisor's memory
    context_str = get_relevant_context(globals.MEMORY_ID, globals.SESSION_ID, max_turns=5)
    
    # Augment query with context if available
    if context_str:
        logger.info(f"✓ Passing {len(context_str)} chars of context to vulnerability analyst")
        enhanced_query = f"Previous conversation:\n{context_str}\n\nCurrent query: {query}"
    else:
        logger.info("No context available for vulnerability analyst")
        enhanced_query = query
    
    # Create stateless agent (no memory)
    context = SimpleNamespace(memory_id=None, session_id=globals.SESSION_ID)
    vuln_agent = gather_vulnerabilities(context)
    result = vuln_agent(enhanced_query)
    return result.message['content'][0]['text']

@tool
def consult_patch_manager(query: str) -> str:
    """Get patch deployment plan from infrastructure specialist."""
    from helper import globals
    from helper.context_helper import get_relevant_context
    from helper.decision_logger import get_decision_logger
    from types import SimpleNamespace
    
    # Log routing decision
    decision_logger = get_decision_logger("supervisor")
    decision_logger.log_routing_decision(
        user_query=query,
        specialist="patch_manager",
        keywords_matched=["patch", "instance", "deploy"],  # Simplified - in production, extract actual keywords
        confidence="high"
    )
    
    # Get recent context from supervisor's memory
    context_str = get_relevant_context(globals.MEMORY_ID, globals.SESSION_ID, max_turns=5)
    
    # Augment query with context if available
    if context_str:
        logger.info(f"✓ Passing {len(context_str)} chars of context to patch manager")
        enhanced_query = f"Previous conversation:\n{context_str}\n\nCurrent query: {query}"
    else:
        logger.info("No context available for patch manager")
        enhanced_query = query
    
    # Create stateless agent (no memory)
    context = SimpleNamespace(memory_id=None, session_id=globals.SESSION_ID)
    patch_agent = patching_operations(context)
    result = patch_agent(enhanced_query)
    return result.message['content'][0]['text']

@tool
def consult_compliance_analyst(query: str) -> str:
    """Get compliance reporting and audit trail analysis."""
    from helper import globals
    from helper.context_helper import get_relevant_context
    from helper.decision_logger import get_decision_logger
    from types import SimpleNamespace
    
    # Log routing decision
    decision_logger = get_decision_logger("supervisor")
    decision_logger.log_routing_decision(
        user_query=query,
        specialist="compliance_analyst",
        keywords_matched=["compliance", "report", "audit"],  # Simplified - in production, extract actual keywords
        confidence="high"
    )
    
    # Get recent context from supervisor's memory
    context_str = get_relevant_context(globals.MEMORY_ID, globals.SESSION_ID, max_turns=5)
    
    # Augment query with context if available
    if context_str:
        logger.info(f"✓ Passing {len(context_str)} chars of context to compliance analyst")
        enhanced_query = f"Previous conversation:\n{context_str}\n\nCurrent query: {query}"
    else:
        logger.info("No context available for compliance analyst")
        enhanced_query = query
    
    # Create stateless agent (no memory)
    context = SimpleNamespace(memory_id=None, session_id=globals.SESSION_ID)
    compliance_agent = analyze_compliance(context)
    result = compliance_agent(enhanced_query)
    return result.message['content'][0]['text']

@app.entrypoint
async def agent_invocation(payload, context):
    """Handler for agent invocation with AgentCore memory integration."""
    from helper.agent_factory import create_agent
    from types import SimpleNamespace
    
    request_start = time.time()
    
    user_message = payload.get("prompt", "Hello! I'm ready to help with intelligent patch automation.")
    logger.info(f"New request: {user_message[:100]}")
    
    # Load memory_id from AgentCore config (set during agentcore launch)
    # Falls back to environment variable for local testing
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
    
    # Create context with memory_id
    agent_context = SimpleNamespace(
        memory_id=memory_id,
        session_id=context.session_id
    )
    
    # Create agent with factory
    agent = create_agent(
        name="patch-automation-supervisor",
        system_prompt=SYSTEM_PROMPT,
        tools=[consult_vulnerability_analyst, consult_patch_manager, consult_compliance_analyst, use_aws],
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
