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

RULES:
- Trust specialist responses — do not reinterpret or fabricate data
- If query is ambiguous (e.g., "patch test"), ask: which environment? which CVE?
- RETRYABLE errors: retry specialist once. ABORT errors: report to user immediately.
- Batch use_aws calls when possible (filters > loops)

OUTPUT:
- Lead with status emoji (✅ ⚠️ ❌) and 1-line summary
- Tables for >3 items (max 3 columns). Lists >10: show first 10 with "Showing 10 of X"
- Target <500 words. No conditional language.
- Always end with "## 🎯 Next Steps" (exactly this heading)

RESPONSE TEMPLATES — pick the template matching the specialist you just consulted:

AFTER vulnerability analysis:
## 🎯 Next Steps
1. Patch [#1 ranked CVE from table] on [affected instances] starting in dev — this is the highest CVSS, widest blast radius, most compliance exposure
2. Create staged patch plan: dev → staging → prod for remaining [N] HIGH CVEs across [N] instances
3. [If compliance-tagged instances exist]: Emergency-patch [instance] ([framework]) — SLA [N]hr
4. Schedule [N] MEDIUM CVEs for next maintenance window

CRITICAL: Step 1 MUST reference the #1 CVE from the ranked table above (highest CVSS × most instances × most compliance frameworks). Never skip it in favor of a lower-ranked CVE.

AFTER patch execution:
## 🎯 Next Steps
1. Verify health checks passed for [instance(s)] in [environment]
2. Proceed with [next environment] patching? (dev → staging → prod)
3. Generate compliance report for [CVE] in [environment]
4. [If failures]: Rollback [instance] and investigate [error]

AFTER compliance report:
## 🎯 Next Steps
1. Address [N] SLA breaches in [environment] — [severity] patches overdue by [N]hr
2. Review [environment] patching trend: [↑↓→] [N]% vs previous period
3. Schedule recurring scan for [environment] to track remediation progress

Rules for next steps:
- Always use "## 🎯 Next Steps" as the heading (exactly this)
- Numbered list, 2-5 items, ordered by urgency
- Each item starts with a verb (Patch, Create, Verify, Proceed, Generate, Review, Address, Schedule, Rollback)
- Each item includes specific targets (instance IDs, CVE IDs, environment names)
- No sub-lists, no "Priority X" labels, no prose paragraphs between items
- No duplicate numbering — single sequence 1, 2, 3...
- Actionable items the user can execute via chat should be phrased as questions
- Always follow staged approach: dev → staging → prod (never jump to prod first)
- For CRITICAL/HIGH CVEs with SLA pressure, recommend emergency targeted patching
- For MEDIUM/LOW, recommend scheduling in next maintenance window"""

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
    context_str = get_relevant_context(globals.MEMORY_ID, globals.SESSION_ID)
    
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
    context_str = get_relevant_context(globals.MEMORY_ID, globals.SESSION_ID)
    
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
    context_str = get_relevant_context(globals.MEMORY_ID, globals.SESSION_ID)
    
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
