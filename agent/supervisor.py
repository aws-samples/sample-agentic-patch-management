"""Supervisor Agent with orchestration capabilities"""

import os
import time
import logging
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import tool
from vulnerability_agent import gather_vulnerabilities
from patch_manager import patching_operations
from use_aws_tool import use_aws

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Profiling flag - set to False for production
ENABLE_PROFILING = os.environ.get("ENABLE_PROFILING", "false").lower() == "true"

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
- AWS services (Lambda, S3, CloudWatch, RDS, billing, cost) → use_aws

TOOLS:
- consult_vulnerability_analyst(query): CVE analysis, CVSS scoring, security risk assessment
- consult_patch_manager(query): EC2 patching, health checks, rollback, maintenance windows
- use_aws(service, operation, parameters): Direct AWS API calls

OUT_OF_SCOPE HANDLING:
If specialist response contains "OUT_OF_SCOPE", immediately retry with use_aws tool.

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
    start_time = time.time()
    if ENABLE_PROFILING:
        logger.info(f"[PROFILE] Supervisor → Vulnerability Analyst: START")
    
    try:
        vuln_agent = gather_vulnerabilities()
        result = vuln_agent(query)
        
        if ENABLE_PROFILING:
            elapsed = time.time() - start_time
            logger.info(f"[PROFILE] Vulnerability Analyst: COMPLETE in {elapsed:.2f}s")
        
        return result.message['content'][0]['text']
    except Exception as e:
        if ENABLE_PROFILING:
            elapsed = time.time() - start_time
            logger.error(f"[PROFILE] Vulnerability Analyst: ERROR in {elapsed:.2f}s: {e}")
        else:
            logger.error(f"Vulnerability Analyst error: {e}")
        return f"Error consulting vulnerability analyst: {str(e)}. Please try again or rephrase your query."

@tool  
def consult_patch_manager(query: str) -> str:
    """Get patch deployment plan from infrastructure specialist."""
    start_time = time.time()
    if ENABLE_PROFILING:
        logger.info(f"[PROFILE] Supervisor → Patch Manager: START")
    
    try:
        patch_agent = patching_operations()
        result = patch_agent(query)
        
        if ENABLE_PROFILING:
            elapsed = time.time() - start_time
            logger.info(f"[PROFILE] Patch Manager: COMPLETE in {elapsed:.2f}s")
        
        return result.message['content'][0]['text']
    except Exception as e:
        if ENABLE_PROFILING:
            elapsed = time.time() - start_time
            logger.error(f"[PROFILE] Patch Manager: ERROR in {elapsed:.2f}s: {e}")
        else:
            logger.error(f"Patch Manager error: {e}")
        return f"Error consulting patch manager: {str(e)}. Please try again or rephrase your query."

@app.entrypoint
async def agent_invocation(payload, context):
    """Handler for agent invocation with AgentCore memory integration."""
    from agent_factory import create_agent
    
    request_start = time.time()
    user_message = payload.get("prompt", "Hello! I'm ready to help with intelligent patch automation.")
    
    if ENABLE_PROFILING:
        logger.info(f"[PROFILE] REQUEST START: {user_message[:50]}...")
    
    # Create agent with factory
    agent = create_agent(
        name="patch-automation-supervisor",
        system_prompt=SYSTEM_PROMPT,
        tools=[consult_vulnerability_analyst, consult_patch_manager, use_aws],
        context=context
    )
    
    supervisor_start = time.time()
    if ENABLE_PROFILING:
        logger.info(f"[PROFILE] Supervisor thinking: START")
    
    # Stream response
    stream = agent.stream_async(user_message)
    first_token = True
    async for event in stream:
        if first_token and ENABLE_PROFILING:
            first_token_time = time.time() - supervisor_start
            logger.info(f"[PROFILE] Supervisor first token: {first_token_time:.2f}s")
            first_token = False
        yield event
    
    if ENABLE_PROFILING:
        total_time = time.time() - request_start
        logger.info(f"[PROFILE] REQUEST COMPLETE: {total_time:.2f}s total")

if __name__ == "__main__":
    app.run()
