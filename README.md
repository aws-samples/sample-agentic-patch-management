# Intelligent Patch Automation

Multi-agent system for automated vulnerability-driven patch management using Strands SDK, Amazon Bedrock AgentCore, and AWS Systems Manager.

## What It Does

When a critical vulnerability is discovered, this system automatically:
1. Analyzes the CVE threat level and business impact
2. Discovers affected EC2 instances across environments
3. Executes phased patching (dev → staging → production) with approval gates
4. Monitors health and recommends rollback if issues detected

**Key differentiator**: Agents make intelligent decisions based on context, not just execute scripts.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                 Patch Automation Supervisor                 │
│              (Coordinates multi-agent workflow)             │
├─────────────────────┬───────────────────────────────────────┤
│  Vulnerability      │           Patch Manager               │
│  Analyst            │                                       │
│                     │                                       │
│ • CVE Analysis      │ • Infrastructure Discovery            │
│ • Threat Assessment │ • Environment Classification          │
│ • Risk Scoring      │ • Patch Execution                     │
│                     │ • Health Checks & Rollback            │
└─────────────────────┴───────────────────────────────────────┘
```

**Technology**: Strands SDK (Python) + Amazon Bedrock AgentCore + AWS CDK

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

## What's Implemented

### ✅ Core Capabilities
- **3 Agents**: Supervisor, Vulnerability Analyst, Patch Manager
- **9 AWS Tools**: EC2 discovery, SSM patching, Inspector integration, health checks, rollback
- **Phased Rollout**: dev → staging → prod with human approval between environments
- **Health Monitoring**: SSM connectivity checks after patching
- **Rollback**: User-approved rollback via `yum history undo`
- **AWS Config Integration**: Resource dependency tracking

### ✅ Infrastructure (AWS CDK)
- **Networking Stack**: VPC, subnets, routing
- **Security Stack**: Security groups, IAM roles, AWS Config
- **Demo Stack**: 15 EC2 instances (5 dev, 5 staging, 5 prod)
- **Permissions Stack**: AgentCore IAM policies

### ✅ Workflows
- **Information Queries**: "What is CVE-X?", "Show me my instances"
- **Vulnerability Analysis**: CVE assessment with CVSS scoring
- **Patch Execution**: Phased deployment with approval gates
- **Health Checks**: Post-patch verification
- **Rollback**: Restore previous package versions

## Project Structure

```
├── agent/                          # Strands agents
│   ├── supervisor.py               # Orchestration
│   ├── vulnerability_agent.py      # Security analysis
│   ├── patch_manager.py            # Patching operations
│   ├── tools.py                    # 9 AWS integration tools
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
# Patch single environment
agentcore invoke '{"prompt": "Patch CVE-2025-12345 in dev environment"}'

# Phased rollout (requires approval between environments)
agentcore invoke '{"prompt": "Handle CVE-2025-12345 across all environments"}'
# Agent patches dev, waits for "proceed", then staging, then prod
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

## Current Limitations

- **No maintenance window integration** - all patching is immediate (planned enhancement)
- **No compliance reporting** - no audit trail generation (planned enhancement)
- **Manual approval required** - between each environment in phased rollout
- **SSM-only health checks** - connectivity-based, not application-aware
- **Linux-only** - yum-based rollback for Amazon Linux/RHEL

## What's Next

See `doc/new-patching-flow.md` for planned enhancements:
- Maintenance window integration (scheduled vs emergency patching)
- Risk-based scheduling (CVSS-driven acceptable delay calculation)
- Compliance report generation (audit trail for patching decisions)
- S3-based report storage

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
**Architecture**: 3-agent system with phased rollout + health checks + rollback  
**Next**: Maintenance window integration & compliance reporting
