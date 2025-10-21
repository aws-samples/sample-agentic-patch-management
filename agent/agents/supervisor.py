"""Supervisor Agent with orchestration capabilities"""

import os
import time
import logging
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import tool
from agents.vulnerability_agent import gather_vulnerabilities
from agents.patch_manager import patching_operations
from agents.compliance_analyst import analyze_compliance
from tools.aws_tools import use_aws
from utils.profiling import profile, log_profile, ENABLE_PROFILING

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

ROUTING:
- Security queries (CVE, vulnerability, CVSS, threat, Inspector) → consult_vulnerability_analyst
- Infrastructure queries (patch, instance, health, rollback, EC2, SSM, maintenance) → consult_patch_manager
- Compliance queries (report, SLA, breach, audit, compliance) → consult_compliance_analyst
- AWS services (Lambda, S3, CloudWatch, RDS, billing, cost) → use_aws

PRIORITY: If query contains "patch", route to consult_patch_manager even if it also mentions compliance/audit

TOOLS:
- consult_vulnerability_analyst(query): CVE analysis, CVSS scoring, security risk assessment
- consult_patch_manager(query): EC2 patching, health checks, rollback, maintenance windows
- consult_compliance_analyst(query): Compliance reports, SLA tracking, audit trails
- use_aws(service, operation, parameters): Direct AWS API calls

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
@profile("Supervisor → Vulnerability Analyst")
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
@profile("Supervisor → Patch Manager")
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
@profile("Supervisor → Compliance Analyst")
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
    
    user_message = payload.get("prompt", "Hello! I'm ready to help with intelligent patch automation.")
    log_profile(f"REQUEST START: {user_message[:50]}...")
    
    # Create agent with factory
    agent = create_agent(
        name="patch-automation-supervisor",
        system_prompt=SYSTEM_PROMPT,
        tools=[consult_vulnerability_analyst, consult_patch_manager, consult_compliance_analyst, use_aws],
        context=context
    )
    
    log_profile("Supervisor thinking: START")
    
    # Stream response
    stream = agent.stream_async(user_message)
    first_token = True
    async for event in stream:
        if first_token and ENABLE_PROFILING:
            log_profile("Supervisor first token received")
            first_token = False
        yield event
    
    log_profile("REQUEST COMPLETE")

if __name__ == "__main__":
    app.run()
