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

# Configure environment
os.environ["BYPASS_TOOL_CONSENT"] = os.environ.get("BYPASS_TOOL_CONSENT", "true")
os.environ["STRANDS_TOOL_CONSOLE_MODE"] = "disabled"

# Initialize AgentCore app
app = BedrockAgentCoreApp()

SYSTEM_PROMPT = """You are a Patch Automation Supervisor that coordinates intelligent vulnerability response through specialist consultation.

Provide your final answer within <answer></answer> tags.

ROUTING DECISION TREE - FOLLOW STRICTLY:

STEP 1 - CLASSIFY QUERY INTENT:
Examine the query for keywords and intent:

A. SECURITY/VULNERABILITY queries contain: "CVE", "vulnerability", "vulnerabilities", "CVSS", "security", "threat", "exploit", "Inspector findings"
   → Route to: consult_vulnerability_analyst

B. INFRASTRUCTURE/PATCHING queries contain: "patch", "deploy", "instance", "instances", "health", "rollback", "SSM", "maintenance window", "EC2"
   → Route to: consult_patch_manager

C. AWS SERVICE queries contain: "Lambda", "S3", "CloudWatch", "RDS", "DynamoDB", "billing", "cost", "CloudFormation"
   → Route to: use_aws (direct, no specialist)

D. AMBIGUOUS queries: Try most likely specialist based on context

STEP 2 - HANDLE OUT_OF_SCOPE RESPONSES:
If specialist returns response containing "OUT_OF_SCOPE":
→ Immediately retry with: use_aws

STEP 3 - EXECUTION RULES:
- Call ONE tool at a time (no parallel calls for simple queries)
- Wait for response before deciding next action
- Only call multiple specialists if query explicitly needs both domains

SPECIALIST TOOLS:
- consult_vulnerability_analyst: CVE analysis, CVSS scoring, security risk assessment, Inspector queries
- consult_patch_manager: EC2 patching, SSM operations, health checks, rollback, maintenance windows
- use_aws: Any AWS service not covered by specialists (Lambda, S3, CloudWatch, billing, etc.)

EFFICIENCY GUIDELINES:
- For simple information queries, consult ONE specialist and return their response immediately
- Only coordinate multiple specialists when the request requires cross-functional work
- Avoid unnecessary back-and-forth - trust specialist expertise

ROUTING EXAMPLES:

SECURITY QUERIES (→ vulnerability_analyst):
- "What is CVE-2024-1234?" 
- "Show me critical vulnerabilities"
- "Analyze security threats"

INFRASTRUCTURE QUERIES (→ patch_manager):
- "Show my EC2 instances"
- "Check health of dev environment"
- "Patch production servers"

AWS SERVICE QUERIES (→ use_aws directly):
- "List Lambda functions"
- "Show S3 buckets"
- "What's my AWS bill?"
- "Check CloudWatch metrics"

FALLBACK HANDLING:
User: "List Lambda functions"
You: [Classify: AWS SERVICE] → use_aws(service_name="lambda", operation_name="list_functions", ...)

User: "Show vulnerabilities"  
You: [Classify: SECURITY] → consult_vulnerability_analyst("Show vulnerabilities")
Specialist: [Returns vulnerability data OR "OUT_OF_SCOPE"]
If OUT_OF_SCOPE: → use_aws as fallback

COMPLEX WORKFLOWS (multi-phase execution):
- "Handle CVE-XXXX" → Delegate to patch_manager for phased execution
- "Patch this vulnerability" → Delegate to patch_manager for phased execution
- "Proceed" or "Continue" → Delegate continuation to patch_manager

PHASED ROLLOUT:
Patch Manager handles dev → staging → prod across multiple invocations.
User says "proceed" between environments.

Present results concisely within <answer></answer> tags."""

@tool
def consult_vulnerability_analyst(query: str) -> str:
    """Get vulnerability analysis from security specialist."""
    start_time = time.time()
    logger.info(f"[PROFILE] Supervisor → Vulnerability Analyst: START")
    
    vuln_agent = gather_vulnerabilities()
    result = vuln_agent(query)
    
    elapsed = time.time() - start_time
    logger.info(f"[PROFILE] Vulnerability Analyst: COMPLETE in {elapsed:.2f}s")
    
    return result.message['content'][0]['text']

@tool  
def consult_patch_manager(query: str) -> str:
    """Get patch deployment plan from infrastructure specialist."""
    start_time = time.time()
    logger.info(f"[PROFILE] Supervisor → Patch Manager: START")
    
    patch_agent = patching_operations()
    result = patch_agent(query)
    
    elapsed = time.time() - start_time
    logger.info(f"[PROFILE] Patch Manager: COMPLETE in {elapsed:.2f}s")
    
    return result.message['content'][0]['text']

@app.entrypoint
async def agent_invocation(payload, context):
    """Handler for agent invocation with AgentCore memory integration."""
    from agent_factory import create_agent
    
    request_start = time.time()
    user_message = payload.get("prompt", "Hello! I'm ready to help with intelligent patch automation.")
    logger.info(f"[PROFILE] REQUEST START: {user_message[:50]}...")
    
    # Create agent with factory
    agent = create_agent(
        name="patch-automation-supervisor",
        system_prompt=SYSTEM_PROMPT,
        tools=[consult_vulnerability_analyst, consult_patch_manager, use_aws],
        context=context
    )
    
    supervisor_start = time.time()
    logger.info(f"[PROFILE] Supervisor thinking: START")
    
    # Stream response
    stream = agent.stream_async(user_message)
    first_token = True
    async for event in stream:
        if first_token:
            first_token_time = time.time() - supervisor_start
            logger.info(f"[PROFILE] Supervisor first token: {first_token_time:.2f}s")
            first_token = False
        yield event
    
    total_time = time.time() - request_start
    logger.info(f"[PROFILE] REQUEST COMPLETE: {total_time:.2f}s total")

if __name__ == "__main__":
    app.run()
