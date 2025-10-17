# Intelligent Patch Automation

Multi-agent system for automated vulnerability-driven patch management using Strands SDK, Amazon Bedrock AgentCore, and AWS Systems Manager.

## What It Does

When a critical vulnerability is discovered, this system automatically:
1. Analyzes the CVE threat level and business impact
2. Checks maintenance windows and calculates risk-based scheduling
3. Decides between scheduled patching (wait for window) or emergency patching (immediate)
4. Discovers affected EC2 instances across environments
5. Executes phased patching (dev → staging → production) with approval gates
6. Monitors health and recommends rollback if issues detected
7. Generates compliance reports with audit trail

**Key differentiator**: Agents make intelligent, risk-based decisions using maintenance windows and CVSS scores, not just execute scripts.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                 Patch Automation Supervisor                 │
│         (Intelligent routing with fallback logic)           │
├─────────────────────┬───────────────────────────────────────┤
│  Vulnerability      │           Patch Manager               │
│  Analyst            │                                       │
│                     │                                       │
│ • CVE Analysis      │ • Infrastructure Discovery            │
│ • Threat Assessment │ • Environment Classification          │
│ • Risk Scoring      │ • Patch Execution                     │
│                     │ • Health Checks & Rollback            │
└─────────────────────┴───────────────────────────────────────┘
                              ↓
                    use_aws (Fallback Tool)
              For AWS services outside specialist scope
```

**Technology**: Strands SDK (Python) + Amazon Bedrock AgentCore + AWS CDK

## Recent Improvements

### Intelligent Routing (Oct 2025)
- **OUT_OF_SCOPE Logic**: Specialists explicitly reject queries outside their domain
- **Decision Tree**: Keyword-based routing for deterministic specialist selection
- **Fallback Pattern**: use_aws tool for AWS services not covered by specialists
- **Performance**: 18-36% faster by eliminating wrong specialist calls

### Production Hardening (Oct 2025)
- **Error Handling**: Graceful degradation when specialists fail
- **Memory Management**: Removed hardcoded memory IDs for multi-user support
- **Conditional Profiling**: Performance logging via `ENABLE_PROFILING` flag

## Quick Start

```bash
# 1. Clone and setup
git clone <repository-url>
cd reinvent25-cop407-agentic-patch-management
./deploy.sh

# 2. Test the agent
cd agent
agentcore invoke '{"prompt": "How many EC2 instances do I have?"}'

# 3. Run a patching workflow
agentcore invoke '{"prompt": "Handle CVE-2025-12345 in dev environment"}'
```

**Prerequisites**: Python 3.9+, AWS CLI, Bedrock AgentCore CLI, AWS account with SSM-managed instances

## Configuration

### Enable Performance Profiling
```bash
# For debugging and performance analysis
export ENABLE_PROFILING=true
agentcore launch

# Profiling disabled by default for production
```

**What profiling shows:**
- Supervisor routing time
- Specialist execution time
- Tool execution time
- Total request latency

**Use for:** Performance debugging, optimization analysis, demo preparation

### AWS Profile
Configure with: `aws configure --profile ri25-demo`

### Agent Runtime
Deployed to Amazon Bedrock AgentCore in `us-east-1`

## What's Implemented

### ✅ Core Capabilities
- **3 Agents**: Supervisor, Vulnerability Analyst, Patch Manager
- **Intelligent Routing**: Keyword-based classification with OUT_OF_SCOPE fallback
- **12 AWS Tools**: EC2 discovery, SSM patching, Inspector integration, health checks, rollback, maintenance windows, compliance reporting, dependency analysis
- **Fallback Tool**: use_aws for AWS services outside specialist scope (Lambda, S3, CloudWatch, etc.)
- **Maintenance Window Integration**: Risk-based scheduling with CVSS-driven acceptable delay calculation
- **Dependency Analysis**: AWS Config-based blast radius assessment with load balancer health tracking
- **Phased Rollout**: dev → staging → prod with human approval between environments
- **Health Monitoring**: SSM connectivity checks after patching
- **Rollback**: User-approved rollback via `yum history undo`
- **Compliance Reporting**: Automated audit trail generation with S3 storage
- **AWS Config Integration**: Resource dependency tracking and relationship discovery
- **Error Handling**: Graceful degradation when specialists fail
- **Performance Profiling**: Optional timing instrumentation via ENABLE_PROFILING flag

### ✅ Infrastructure (AWS CDK)
- **Networking Stack**: VPC, subnets, routing
- **Security Stack**: Security groups, IAM roles, AWS Config, S3 compliance reports bucket
- **Demo Stack**: 15 EC2 instances (5 dev, 5 staging, 5 prod) with maintenance windows
- **Permissions Stack**: AgentCore IAM policies (EC2, SSM, Inspector, Config, S3)

### ✅ Workflows
- **Information Queries**: "What is CVE-X?", "Show me my instances"
- **Vulnerability Analysis**: CVE assessment with CVSS scoring
- **Maintenance-Aware Patching**: Risk-based scheduling (scheduled vs emergency modes)
- **Patch Execution**: Phased deployment with approval gates
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
│   ├── supervisor.py               # Orchestration
│   ├── vulnerability_agent.py      # Security analysis
│   ├── patch_manager.py            # Patching operations
│   ├── tools.py                    # 12 AWS integration tools
│   ├── agent_factory.py            # Common agent creation (DRY)
│   ├── config/
│   │   └── sla_policy.json         # Compliance framework SLA mappings
│   └── .bedrock_agentcore.yaml     # AgentCore config
├── infra/                          # AWS CDK infrastructure
│   ├── lib/
│   │   ├── networking-stack.ts
│   │   ├── security-stack.ts
│   │   ├── agentcore-permissions-stack.ts
│   │   └── vulnerability-driven-patching-stack.ts
│   └── bin/app.ts
├── doc/                            # Documentation
│   ├── new-patching-flow.md        # Maintenance window integration plan
│   ├── updated_idea.md             # Enhanced workflow design
│   └── testing guides...
├── deploy.sh                       # One-command deployment
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

**AWS Profile**: `ri25-demo` (configure with `aws configure --profile ri25-demo`)

**Agent Runtime**: Deployed to Amazon Bedrock AgentCore in `us-east-1`

**Demo Instances**: 15 EC2 instances tagged by environment (dev/staging/prod)

**Maintenance Windows**: 
- dev-daily-patching: Daily at 1 AM UTC
- staging-weekly-patching: Tuesday at 2 AM UTC  
- prod-monthly-patching: 1st of month at 2 AM UTC

## Current Limitations

- **Demo mode behavior** - Agent may proceed between environments without strict status verification to maintain demo flow (5-10 min patching waits). Production use requires verify_and_proceed() tool implementation.
- **Manual approval required** - between each environment in phased rollout
- **SSM-only health checks** - connectivity-based, not application-aware
- **Linux-only** - yum-based rollback for Amazon Linux/RHEL

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

**Status**: ✅ Core functionality complete  
**Architecture**: 3-agent system with maintenance-aware patching + phased rollout + health checks + rollback + compliance reporting
