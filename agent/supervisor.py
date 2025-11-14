"""Supervisor Agent with orchestration capabilities"""

import os
import time
import logging
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import tool
from vulnerability_agent import gather_vulnerabilities
from patch_manager import patching_operations
from compliance_analyst import analyze_compliance
from tools import use_aws

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
- If data is truncated (_truncated: true), just say "Showing X of Y"
- Target: <500 words per response

ROUTING:
- AWS services (Lambda, S3, CloudWatch, RDS, DynamoDB, IAM, VPC, billing, cost) → use_aws ONLY
- Security queries (CVE, vulnerability, CVSS, threat, Inspector) → consult_vulnerability_analyst
- Infrastructure queries (patch, instance, health, rollback, EC2, SSM, maintenance) → consult_patch_manager
- Compliance queries (report, SLA, breach, audit, compliance) → consult_compliance_analyst

PRIORITY: If query contains "patch", route to consult_patch_manager even if it also mentions compliance/audit

TOOLS:
- use_aws(service, operation, parameters): Direct AWS API calls
- consult_vulnerability_analyst(query): CVE analysis, CVSS scoring, security risk assessment
- consult_patch_manager(query): EC2 patching, health checks, rollback, maintenance windows
- consult_compliance_analyst(query): Compliance reports, SLA tracking, audit trails

OUT_OF_SCOPE HANDLING:
If specialist response contains "OUT_OF_SCOPE", immediately retry with use_aws tool.

USE_AWS OPTIMIZATION:
When using use_aws tool, optimize for efficiency:
- Batch related queries when possible (e.g., describe multiple resources in one call)
- Use filters to reduce data transfer (e.g., tag filters, state filters)
- Prefer single calls with multiple resource IDs over loops
- Example: describe_instances with Filters instead of multiple describe_instances calls

EXECUTION:
- Call one tool at a time
- Trust specialist responses
- For "proceed" or "continue" commands, delegate to patch_manager (handles phased rollout state)

OUTPUT FORMAT:
- Lead with status/summary in CAPS or with emoji (✅ ⚠️ ❌)
- Use numbered lists (1. 2. 3.) or bullets (- •) for items
- Separate sections with blank lines
- End with clear next steps or questions"""

@tool
def consult_vulnerability_analyst(query: str) -> str:
    """Get vulnerability analysis from security specialist."""
    # Create context object from globals
    import globals
    from types import SimpleNamespace
    context = SimpleNamespace(memory_id=globals.MEMORY_ID, session_id=globals.SESSION_ID)
    
    vuln_agent = gather_vulnerabilities(context)
    result = vuln_agent(query)
    return result.message['content'][0]['text']

@tool
def consult_patch_manager(query: str) -> str:
    """Get patch deployment plan from infrastructure specialist."""
    # Create context object from globals
    import globals
    from types import SimpleNamespace
    context = SimpleNamespace(memory_id=globals.MEMORY_ID, session_id=globals.SESSION_ID)
    
    patch_agent = patching_operations(context)
    result = patch_agent(query)
    return result.message['content'][0]['text']

@tool
def consult_compliance_analyst(query: str) -> str:
    """Get compliance reporting and audit trail analysis."""
    # Create context object from globals
    import globals
    from types import SimpleNamespace
    context = SimpleNamespace(memory_id=globals.MEMORY_ID, session_id=globals.SESSION_ID)
    
    compliance_agent = analyze_compliance(context)
    result = compliance_agent(query)
    return result.message['content'][0]['text']

@app.entrypoint
async def agent_invocation(payload, context):
    """Handler for agent invocation with AgentCore memory integration."""
    from agent_factory import create_agent
    from types import SimpleNamespace
    
    request_start = time.time()
    
    user_message = payload.get("prompt", "Hello! I'm ready to help with intelligent patch automation.")
    logger.info(f"New request: {user_message[:100]}")
    
    # Create context with memory_id from config
    agent_context = SimpleNamespace(
        memory_id="intelligent_patch_automation_mem-La8v5RAIoS",
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
