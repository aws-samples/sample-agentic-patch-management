# Intelligent Patch Automation

Multi-agent system for automated vulnerability-driven patch management using Strands SDK, Amazon Bedrock AgentCore, and AWS Systems Manager.

---

## Why This Matters

**Traditional patching:** 15 browser tabs, 3 CLI tools, 45 minutes of manual work per CVE.

**This approach:** Natural language query → intelligent decision → automated execution.

```bash
You: Handle CVE-2025-38527 in dev environment
🤖 Agent: Checking maintenance window... Emergency patching required.
         Patching dev → staging → prod with approval gates.
         ✅ Complete. Compliance report saved to S3.
```

**Key differentiator:** Agents make intelligent, risk-based decisions using maintenance windows and CVSS scores, not just execute scripts.

**Built with:** AWS native services (Bedrock AgentCore, SSM, Inspector, Config). No new infrastructure to manage.

---

## What It Does

When a critical vulnerability is discovered, this system automatically:
1. Analyzes the CVE threat level and business impact
2. Checks maintenance windows and calculates risk-based scheduling
3. Decides between scheduled patching (wait for window) or emergency patching (immediate)
4. Discovers affected EC2 instances across environments
5. Executes phased patching (dev → staging → production) with approval gates
6. Monitors health via CloudWatch alarms and recommends rollback if issues detected
7. Generates compliance reports with audit trail

**Key differentiator**: Agents make intelligent, risk-based decisions using maintenance windows and CVSS scores, not just execute scripts. Application-layer health monitoring via CloudWatch alarms ensures safe patching operations.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                 Patch Automation Supervisor                 │
│         (Intelligent routing with fallback logic)           │
├─────────────┬───────────────────┬───────────────────────────┤
│ Vulnerability│   Patch Manager   │   Compliance Analyst     │
│ Analyst      │                   │                          │
│              │                   │                          │
│ • CVE        │ • Infrastructure  │ • Report Queries         │
│   Analysis   │   Discovery       │ • SLA Tracking           │
│ • Threat     │ • Patch Execution │ • Audit Trails           │
│   Assessment │ • Health Checks   │ • Executive Reporting    │
│ • Risk       │ • Rollback        │ • Trend Analysis         │
│   Scoring    │                   │                          │
└─────────────┴───────────────────┴───────────────────────────┘
                              ↓
                    use_aws (Fallback Tool)
              For AWS services outside specialist scope
```

**Technology**: Strands SDK (Python) + Amazon Bedrock AgentCore + AWS CDK

## Key Features

- **SLA-Aware Patching**: Automatic compliance-based decisions (PCI-DSS, SOC2, HIPAA)
- **Risk-Based Scheduling**: EMERGENCY vs SCHEDULED patching based on maintenance windows
- **Policy Enforcement**: Customer-configurable constraints (time windows, batch limits, approval requirements)
- **Intelligent Routing**: Keyword-based specialist selection with fallback logic
- **Phased Rollout**: dev → staging → prod with approval gates
- **Health Monitoring**: Two-layer checks (infrastructure + application via CloudWatch alarms)
- **Automated Rollback**: User-approved rollback with health verification
- **Decision Transparency**: All decisions logged to CloudWatch + S3 for audit
- **Compliance Reporting**: Automated audit trails with SLA tracking stored in S3
- **Natural Language**: No rigid commands, just conversational queries

## Quick Start

```bash
# 1. Clone and setup
git clone <repository-url>
cd reinvent25-cop407-agentic-patch-management
./deploy.sh

# 2. Use the chat client (recommended)
./chat.sh

# 3. Or invoke directly
cd agent
agentcore invoke '{"prompt": "How many EC2 instances do I have?"}'
```

**Prerequisites**: Python 3.9+, AWS CLI, Bedrock AgentCore CLI, AWS account with SSM-managed instances

## Chat Client

Lightweight Python client using boto3 to communicate directly with the Bedrock AgentCore runtime. Clean streaming responses with real-time tool indicators.

### Interactive Mode
```bash
./chat.sh                    # Start interactive chat
./chat.sh -p <your-profile>  # Use specific AWS profile
```

### Single Message Mode
```bash
./chat.sh -m "What vulnerabilities do we have?"
./chat.sh -m "Handle CVE-2025-38527" -p <your-profile>
```

### Features
- **Real-time agent activity** - See which specialist is working (🔍 Vulnerability Analyst, 🛠️ Patch Manager, ⚙️ AWS)
- **Streaming responses** - Watch answers appear as they're generated using SSE (Server-Sent Events)
- **Direct boto3 integration** - No CLI subprocess overhead, clean JSON parsing
- **Natural language** - No rigid commands, just ask
- **Session management** - Maintains conversation context across messages

### What You'll See
```
You: What critical vulnerabilities do we have?

🔍 Consulting Vulnerability Analyst...

🤖 Agent: I found 3 critical vulnerabilities in your environment:
1. CVE-2025-38527 (CVSS 9.8)...
```

### Troubleshooting
- **Missing dependencies**: `pip install boto3 pyyaml`
- **Agent not deployed**: `./deploy.sh agent`
- **AWS credentials**: Ensure AWS profile is configured (`aws configure --profile ri25-demo`)

## What's Implemented

### ✅ Core Capabilities
- **4 Agents**: Supervisor, Vulnerability Analyst, Patch Manager, Compliance Analyst
- **Intelligent Routing**: Keyword-based classification with OUT_OF_SCOPE fallback
- **12 AWS Tools**: EC2 discovery, SSM patching, Inspector integration, CloudWatch alarm monitoring, health checks, rollback, maintenance windows, compliance reporting, dependency analysis, report querying — all with structured error responses
- **Fallback Tool**: use_aws for any AWS service with automatic pagination support
- **SLA-Aware Patching**: Compliance framework integration (PCI-DSS, SOC2, HIPAA) with risk-based decisions
- **Maintenance Window Integration**: Risk-based scheduling with CVSS-driven acceptable delay calculation
- **Dependency Analysis**: AWS Config-based blast radius assessment with load balancer health tracking
- **Phased Rollout**: dev → staging → prod with human approval between environments
- **Two-Layer Health Monitoring**: 
  - Infrastructure: SSM connectivity verification
  - Application: CloudWatch alarm monitoring (requires alarms configured)
- **Rollback**: User-approved rollback via `yum history undo` with health verification
- **Compliance Reporting**: Automated audit trail generation with S3 storage and queryable reports
- **Executive Reporting**: Compliance analyst for CISO/CRO/CTO queries (SLA breaches, team performance, trends)
- **AWS Config Integration**: Resource dependency tracking and relationship discovery
- **Structured Error Recovery**: Classified error responses (RETRYABLE, SKIP, ABORT) with agent-driven recovery
- **Decision Audit Trail**: Every routing, SLA, and error recovery decision logged to CloudWatch + S3
- **Observability**: AgentCore native observability with CloudWatch Logs, X-Ray tracing, and GenAI Dashboard

### ✅ Infrastructure (AWS CDK)
- **Networking Stack**: VPC, subnets, routing
- **Security Stack**: Security groups, IAM roles, AWS Config, S3 compliance reports bucket
- **Sample Environment**: 15 EC2 instances (5 dev, 5 staging, 5 prod) with maintenance windows for testing
- **Permissions Stack**: AgentCore IAM policies (EC2, SSM, Inspector, Config, S3)

### ✅ Workflows
- **Information Queries**: "What is CVE-X?", "Show me my instances"
- **Vulnerability Analysis**: CVE assessment with CVSS scoring
- **Maintenance-Aware Patching**: Risk-based scheduling (scheduled vs emergency modes)
- **Patch Execution**: Phased deployment with approval gates
  - Fast mode: User says "proceed" (quick progression, 2-3 min per environment)
  - Verification mode: User says "verify and proceed" (automated verification, 10-12 min per environment)
- **Health Checks**: Post-patch verification
- **Rollback**: Restore previous package versions
- **Compliance Reporting**: Automated audit trail with S3 storage

### ✅ Routing Logic

The Supervisor uses intelligent routing to direct queries to the right specialist:

**Security Queries** → Vulnerability Analyst
- Keywords: CVE, vulnerability, security, CVSS, threat, exploit
- Examples: "Show critical vulnerabilities", "Analyze CVE-2024-1234"

**Infrastructure Queries** → Patch Manager
- Keywords: patch, deploy, instance, health, rollback, SSM, maintenance
- Examples: "Show EC2 instances", "Check health of dev environment"

**AWS Service Queries** → use_aws (direct)
- Keywords: Lambda, S3, CloudWatch, RDS, billing, cost
- Examples: "List Lambda functions", "Show S3 buckets"

**OUT_OF_SCOPE Handling:**
- Specialists explicitly reject queries outside their domain
- Supervisor automatically retries with use_aws fallback
- Ensures queries always get answered

**Performance:** 18-36% faster than previous implementation by eliminating wrong specialist calls.

## Project Structure

```
├── agent/                          # Strands agents
│   ├── supervisor.py               # Orchestration + routing
│   ├── vulnerability_agent.py      # Security analysis
│   ├── patch_manager.py            # Patching operations
│   ├── compliance_analyst.py       # Reporting & audit
│   ├── helper/                     # Shared utilities
│   │   ├── tools.py                # 12 AWS tools + structured error handling
│   │   ├── error_handling.py       # Error classification (RETRYABLE/SKIP/ABORT)
│   │   ├── decision_logger.py      # Decision audit trail (CloudWatch + S3)
│   │   ├── agent_factory.py        # Common agent creation
│   │   ├── memory_hooks.py         # AgentCore memory integration
│   │   └── globals.py              # Shared state
│   ├── config/
│   │   └── config.json             # SLA policies, environment aliases
│   └── .bedrock_agentcore.yaml     # AgentCore config
├── infra/                          # AWS CDK infrastructure
│   ├── lib/
│   │   ├── networking-stack.ts
│   │   ├── security-stack.ts
│   │   ├── agentcore-permissions-stack.ts
│   │   └── vulnerability-driven-patching-stack.ts
│   └── bin/app.ts
├── chatbot-client/                 # Chat client (boto3 + SSE streaming)
├── doc/                            # Documentation
│   ├── architecture.md             # Technical architecture
│   ├── observability.md            # Decision log queries (CloudWatch, Athena, Splunk)
│   ├── PRESENTATION_GUIDE.md       # Live presentation script
│   ├── code_talk.md                # Technical deep-dive talk
│   └── troubleshooting.md          # Operational troubleshooting
├── deploy.sh                       # One-command deployment
├── chat.sh                         # Chat client wrapper
└── README.md                       # This file
```

## Testing Examples

### Basic Queries
```bash
# Instance discovery
agentcore invoke '{"prompt": "List my EC2 instances in dev environment"}'

# Vulnerability lookup
agentcore invoke '{"prompt": "What is CVE-2025-38527?"}'

# Compliance check
agentcore invoke '{"prompt": "Check patch compliance status"}'
```

### Patching Workflow

```bash
# Scheduled patching (MEDIUM severity - waits for maintenance window)
agentcore invoke '{"prompt": "Handle CVE-2025-12345 (CVSS 5.5) in dev environment"}'
# Agent checks maintenance window, recommends waiting for scheduled window

# Emergency patching (CRITICAL severity - immediate action)
agentcore invoke '{"prompt": "Handle CVE-2025-99999 (CVSS 9.8) with active exploitation in prod"}'
# Agent checks maintenance window, determines delay unacceptable, patches immediately

# Phased rollout with status checks
agentcore invoke '{"prompt": "Handle CVE-2025-12345 in dev"}'
# Agent patches dev → user says "check status" → user says "proceed" → staging → prod
```

### Health Check & Rollback
```bash
# Check health after patching
agentcore invoke '{"prompt": "Check health of dev instances"}'

# Rollback if issues detected
agentcore invoke '{"prompt": "Rollback dev patches"}'
```

## Configuration

### SLA Policies (`agent/config/config.json`)

Define SLA requirements by compliance framework:

```json
{
  "frameworks": {
    "PCI-DSS": {"CRITICAL": 12, "HIGH": 48, "MEDIUM": 168, "LOW": 720},
    "SOC2": {"CRITICAL": 24, "HIGH": 72, "MEDIUM": 168, "LOW": 720},
    "HIPAA": {"CRITICAL": 24, "HIGH": 72, "MEDIUM": 168, "LOW": 720}
  }
}
```

Tag instances with `ComplianceFrameworks` to apply SLA policies automatically.

---

### Maintenance Windows (SSM)

The agent **respects** SSM maintenance windows through intelligent decision-making:

**SCHEDULED patching** (window within SLA):
- Agent registers task with maintenance window
- SSM executes during window automatically
- No immediate action

**EMERGENCY patching** (window exceeds SLA):
- Agent patches immediately via send_command
- Bypasses maintenance window
- Logged as emergency action

**Decision logic**:
```
IF hours_until_window <= sla_hours:
  → SCHEDULED (register with maintenance window)
ELSE:
  → EMERGENCY (patch immediately)
```

**Example**:
- SLA: 12hr (CRITICAL PCI-DSS)
- Window: 8hr away
- Decision: SCHEDULED (8 < 12)
- Action: Register with window

---

### Deterministic Workflow

The agent follows a **mandatory 7-step workflow** enforced through system prompt:

```
Step 1: get_patch_compliance(environment, severity)
Step 2: get_maintenance_windows(environment)
Step 3: Calculate decision (SCHEDULED vs EMERGENCY)
Step 4: execute_patch_operation(execution_mode, ...)
Step 5: check_instance_health() [if EMERGENCY]
Step 6: check_cloudwatch_alarms() [if EMERGENCY]
Step 7: generate_compliance_report() [always]
```

**No external validation needed** - workflow is built into the prompt.

**Controls are in SSM**:
- Maintenance windows define when
- Patch baselines define what
- Agent respects both automatically

---

### Observability & Performance Monitoring

AgentCore provides native observability out of the box:

**CloudWatch Logs:**
```bash
aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-name>-DEFAULT --follow
```

**GenAI Observability Dashboard:**
```
https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#gen-ai-observability/agent-core
```

**CloudWatch Insights Queries:**
```
# Request performance
fields @timestamp, @message 
| filter @message like /Request completed/ 
| parse @message "Request completed in *ms" as duration
| stats count() as requests, avg(duration) as avg_ms

# Error tracking
fields @timestamp, @message 
| filter @level = "ERROR"
| stats count() as errors by bin(5m)
```

**What's tracked automatically:**
- Request/response times
- Token usage (input/output)
- Tool invocations
- Error rates
- X-Ray distributed tracing
- **Agent decisions** (logged to CloudWatch + S3 for audit)

**Decision Logs:**
Every agent decision is logged with full context:
```json
{
  "decision_type": "sla_calculation",
  "decision": "EMERGENCY",
  "reasoning": "Window (36hr) exceeds SLA (12hr) by 24 hours",
  "context": {"cve_id": "CVE-2024-1234", "severity": "CRITICAL", ...}
}
```

Stored in CloudWatch (real-time) and S3 (`decisions/YYYY/MM/DD/{agent}/{uuid}.json`) for long-term audit.  
Query with CloudWatch Insights, Athena, or your existing tools (Splunk, Datadog).  
See `doc/observability.md` for query examples.

---### AWS Profile & Runtime

**AWS Profile**: Configure with `aws configure --profile <your-profile>`

**Agent Runtime**: Deployed to Amazon Bedrock AgentCore in `us-east-1`

**Sample Instances**: 15 EC2 instances tagged by environment (dev/staging/prod) for testing

**Maintenance Windows**: 
- dev-daily-patching: Daily at 1 AM UTC
- staging-weekly-patching: Tuesday at 2 AM UTC  
- prod-monthly-patching: 1st of month at 2 AM UTC

## Current Limitations

- **Two execution modes available:**
  - **Fast mode (default):** User says "proceed" → agent moves to next environment immediately (2-3 min per environment)
  - **Verification mode (recommended):** User says "verify and proceed" → agent waits for completion + runs health checks (10-12 min per environment)
- **Manual approval required** - between each environment in phased rollout for safety
- **CloudWatch alarm dependency** - Application health checks require CloudWatch alarms to be configured
- **Linux-only rollback** - yum-based rollback for Amazon Linux/RHEL (Windows patching supported, rollback in development)

## Monitoring

```bash
# View agent logs
aws logs tail /aws/bedrock-agentcore/runtimes/<your-runtime-name>-DEFAULT --follow

# Check agent status
agentcore status

# View recent tool calls
aws logs filter-log-events \
  --log-group-name "/aws/bedrock-agentcore/runtimes/<your-runtime-name>-DEFAULT" \
  --filter-pattern "Tool" \
  --start-time $(date -v-1H +%s)000
```

## Troubleshooting

**Agent not responding**: `agentcore status` then `agentcore launch --auto-update-on-conflict`

**Permission errors**: Check AgentCore permissions stack includes EC2, SSM, Inspector, Config

**No instances found**: Verify instances are SSM-managed and tagged with `Environment` tag

**Detailed guides**: See `doc/troubleshooting.md`

---

