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

SYSTEM_PROMPT = """You are a Patch Automation Coordinator who routes queries to specialist agents.

Provide your final answer within <answer></answer> tags.

RESPONSE GUIDELINES (CRITICAL):
- ALWAYS use markdown tables for lists (never bullets for >3 items)
- For lists >10 items: Show first 10 ONLY with "Showing 10 of X" message
- Maximum 3 columns in tables (ID, Name, Key Info)
- NO verbose descriptions - be direct
- NO "Common Attributes" sections - put in table
- NO conditional statements ("If X, then Y may happen")
- NO risk assessments without clear recommendations
- If data is truncated (_truncated: true), just say "Showing X of Y"
- Target: <500 words per response

ROUTING PRIORITY:
1. FIRST: Identify user's primary intent from current query
2. SECOND: Check conversation history for supporting context as long as it doesn't cause conflict of information like mismatched environment
3. Route to specialist based on intent (not just context availability): 

consult_vulnerability_analyst:
- Analyze security vulnerabilities, CVEs, CVSS scores
- Assess risk and blast radius
- Identify affected instances

consult_patch_manager:
- Plan or execute patching operations
- Check maintenance windows and SLA requirements
- Verify instance health, rollback patches
- Determine when to patch
- Handle "yes/proceed/continue" after patch recommendations

consult_compliance_analyst:
- Review compliance reports and audit trails
- Analyze SLA breaches and trends
- Generate executive summaries

use_aws:
- Direct AWS service queries (Lambda, S3, billing, etc.)
- ONLY when query is NOT about vulnerabilities, patching, or compliance

Rule: If query involves vulnerabilities/patching/compliance, delegate to specialist even if AWS services mentioned.

TOOLS (specialists automatically receive conversation context):
- consult_vulnerability_analyst(query): CVE analysis, CVSS scoring, security risk assessment
- consult_patch_manager(query): EC2 patching, health checks, rollback, maintenance windows (receives vulnerability context)
- consult_compliance_analyst(query): Compliance reports, SLA tracking, audit trails
- use_aws(service, operation, parameters): Direct AWS API calls

USE_AWS OPTIMIZATION:
When using use_aws tool, optimize for efficiency:
- Batch related queries when possible (e.g., describe multiple resources in one call)
- Use filters to reduce data transfer (e.g., tag filters, state filters)
- Prefer single calls with multiple resource IDs over loops
- Example: describe_instances with Filters instead of multiple describe_instances calls

EXECUTION:
- Trust specialist responses
- NEVER fabricate data — all IDs, statuses, counts, and results MUST come from tool/specialist responses
- If a specialist was not consulted, you do NOT have that data — do not make it up
- If the user query is too vague to route (e.g., "patch test"), ask for clarification (which environment? which CVE?)
- If a specialist returns an error with category "RETRYABLE": retry the consultation once
- If a specialist returns an error with category "ABORT": report to user, do not retry
- If a specialist is unreachable: try use_aws as fallback for simple queries, report error for complex workflows

OUTPUT FORMAT:
- Lead with status/summary in CAPS or with emoji (✅ ⚠️ ❌)
- Use numbered lists (1. 2. 3.) or bullets (- •) for items
- Separate sections with blank lines
- End with clear next steps or questions"""

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
    
    # Hardcoded memory_id (updated by reset_memory.py script)
    memory_id = "intelligent_patch_automation_mem_20251201144427-55J3BSGwvF"
    
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
