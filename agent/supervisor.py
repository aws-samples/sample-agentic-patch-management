"""Supervisor Agent with orchestration capabilities"""

import os
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import tool
from vulnerability_agent import gather_vulnerabilities
from patch_manager import patching_operations

# Configure environment
os.environ["BYPASS_TOOL_CONSENT"] = os.environ.get("BYPASS_TOOL_CONSENT", "true")
os.environ["STRANDS_TOOL_CONSOLE_MODE"] = "disabled"

# Initialize AgentCore app
app = BedrockAgentCoreApp()

SYSTEM_PROMPT = """You are a Patch Automation Supervisor that coordinates intelligent vulnerability response through specialist consultation.

Provide your final answer within <answer></answer> tags.

SPECIALIST TOOLS:
- consult_vulnerability_analyst: Security threat analysis and risk assessment
- consult_patch_manager: Infrastructure discovery, deployment operations, and patch execution

EFFICIENCY GUIDELINES:
- For simple information queries, consult ONE specialist and return their response immediately
- Only coordinate multiple specialists when the request requires cross-functional work
- Avoid unnecessary back-and-forth - trust specialist expertise

WORKFLOW DETECTION AND EXECUTION:

SIMPLE QUERIES (single consultation, return immediately):
- "What is CVE-XXXX?" → consult_vulnerability_analyst, return answer
- "Show me vulnerabilities" → consult_vulnerability_analyst, return answer
- "What are critical vulnerabilities?" → consult_vulnerability_analyst, return answer
- "Show infrastructure" → consult_patch_manager, return answer
- "Check health" → consult_patch_manager, return answer

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
    vuln_agent = gather_vulnerabilities()
    result = vuln_agent(query)
    return result.message['content'][0]['text']

@tool  
def consult_patch_manager(query: str) -> str:
    """Get patch deployment plan from infrastructure specialist."""
    patch_agent = patching_operations()
    result = patch_agent(query)
    return result.message['content'][0]['text']

@app.entrypoint
async def agent_invocation(payload, context):
    """Handler for agent invocation with AgentCore memory integration."""
    from agent_factory import create_agent
    
    # Create agent with factory
    agent = create_agent(
        name="patch-automation-supervisor",
        system_prompt=SYSTEM_PROMPT,
        tools=[consult_vulnerability_analyst, consult_patch_manager],
        context=context
    )
    
    user_message = payload.get("prompt", "Hello! I'm ready to help with intelligent patch automation.")
    
    # Stream response
    stream = agent.stream_async(user_message)
    async for event in stream:
        yield event

if __name__ == "__main__":
    app.run()
