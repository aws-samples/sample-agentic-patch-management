# Intelligent Patch Automation

Multi-agent system for automated vulnerability-driven patch management using Strands SDK, Amazon Bedrock AgentCore, and AWS Systems Manager.

---

## Why This Matters

Traditional patching: 15 browser tabs, 3 CLI tools, 45 minutes of manual work per CVE.

This approach: natural language query → intelligent decision → automated execution.

```
You: Handle CVE-2025-38527 in dev environment
Agent: Checking maintenance window... Emergency patching required.
       Patching dev → staging → prod with approval gates.
       Complete. Compliance report saved to S3.
```

Agents make risk-based decisions using maintenance windows and CVSS scores. Application-layer health monitoring via CloudWatch alarms ensures safe patching. Built entirely on AWS native services.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                 Patch Automation Supervisor                  │
│              (Intelligent routing + fallback)                │
├─────────────┬───────────────────┬───────────────────────────┤
│ Vulnerability│   Patch Manager   │   Compliance Analyst      │
│ Analyst      │                   │                           │
│ • CVE        │ • Patch Compliance│ • Report Queries          │
│   Analysis   │ • Patch Execution │ • SLA Tracking            │
│ • Threat     │ • Health Checks   │ • Audit Trails            │
│   Assessment │ • Rollback        │ • Executive Reporting     │
│ • Impact     │ • Maintenance     │ • Trend Analysis          │
│   Analysis   │   Windows         │                           │
└─────────────┴───────────────────┴───────────────────────────┘
```

The supervisor routes queries to the right specialist automatically. If a specialist can't handle a query, the supervisor re-routes or falls back to direct AWS API access via `use_aws`.

### Patching Workflow

Every patching operation follows a mandatory 7-step workflow:

1. Discover affected instances and calculate SLA requirement
2. Check next maintenance window
3. Decide: SCHEDULED (wait for window) or EMERGENCY (patch now)
4. Execute patching via SSM
5. Verify instance health (SSM connectivity)
6. Check application health (CloudWatch alarms)
7. Generate compliance report (saved to S3)

Decision logic: if the next maintenance window is within the SLA deadline → SCHEDULED. If the window would exceed the SLA → EMERGENCY.

---

## Prerequisites

- Python 3.9+
- Node.js 18+ (for CDK)
- Docker (for building the UI container image)
- AWS CLI v2 configured with a named profile
- AWS account with Bedrock model access enabled (Claude Sonnet)

---

## Quick Start

```bash
# 1. Clone and configure
git clone <repository-url>
cd intelligent-patch-automation
cp .env.example .env
# Edit .env — set AWS_PROFILE and AWS_REGION

# 2. Deploy everything
./deploy.sh

# That's it. The script handles:
#   - Python venv + dependencies
#   - AgentCore agent deployment
#   - Auto-detects the AgentCore IAM role ARN
#   - CDK bootstrap + infrastructure (3 stacks)
#   - Docker build + Fargate UI deployment

# To include sample EC2 instances for testing:
./deploy.sh --with-demo
```

---

## Deployment

### Environment Variables

Both `deploy.sh` and `chat.sh` auto-source `.env` from the project root.

| Variable | Required | Description |
|----------|----------|-------------|
| `AWS_PROFILE` | Yes | AWS CLI named profile |
| `AWS_REGION` | Yes | Defaults to `us-east-1` |
| `AGENTCORE_ROLE_ARN` | No | Auto-detected after agent deploy. Set only if you have multiple AgentCore roles. |
| `API_KEY` | No | Enables API key auth on the web UI |

### Deploy Commands

```bash
./deploy.sh               # Full solution: infra + agent + UI
./deploy.sh --with-demo   # Full solution + sample EC2 environment
./deploy.sh agent         # Redeploy agent only (after code changes)
./deploy.sh ui            # Redeploy UI only (after code changes)
./deploy.sh destroy       # Destroy ALL infrastructure
./deploy.sh destroy --demo-only  # Destroy sample environment only
```

The default `./deploy.sh` handles the full deployment sequence automatically: Python venv → agent → role detection → CDK infra → UI. No manual steps between.

### Infrastructure Stacks

| Stack | Type | What It Creates |
|-------|------|-----------------|
| `PatchAutomationNetworking` | Solution | VPC, subnets (public/private/isolated), NAT gateways, flow logs |
| `PatchAutomationSecurity` | Solution | Security groups, instance IAM role, S3 compliance bucket, SQS workflow queue, AWS Config recorder |
| `PatchAutomationAgentCorePermissions` | Solution | IAM policy for AgentCore runtime (EC2, SSM, Inspector, Config, CloudWatch, S3) |
| `PatchAutomationUI` | Solution | ECS Fargate service, internal ALB, CloudWatch logs — serves the Web UI |
| `PatchAutomationVulnerabilityDriven` | Sample Environment | 15 EC2 instances (5 dev/5 staging/5 prod), maintenance windows, ALBs, patch baselines, CloudWatch alarms |

The sample environment stack deploys and destroys independently. Use `./deploy.sh destroy --demo-only` to remove it while keeping the solution infrastructure intact.

If your account already has an AWS Config recorder, pass `-c skipConfigRecorder=true` when deploying:
```bash
cd infra && npx cdk deploy --all -c agentCoreRoleArn="$AGENTCORE_ROLE_ARN" -c skipConfigRecorder=true
```

S3 compliance buckets use `RETAIN` removal policy — they persist after stack deletion to preserve audit data.

### Instance Requirements

For the agent to manage your EC2 instances, they need:
- SSM Agent installed and running
- IAM instance profile with `AmazonSSMManagedInstanceCore`
- Tag: `Environment` (e.g., `dev`, `staging`, `prod`)
- Tag: `ComplianceFrameworks` (optional, e.g., `PCI-DSS,SOC2`) for SLA policy enforcement

The sample environment stack creates instances with all of these pre-configured.

---

## Configuration

### SLA Policies

Defined in `agent/config/config.json`. SLA hours by compliance framework and severity:

| Framework | CRITICAL | HIGH | MEDIUM | LOW |
|-----------|----------|------|--------|-----|
| PCI-DSS | 12h | 48h | 168h | 720h |
| SOC2 | 24h | 72h | 168h | 720h |
| HIPAA | 24h | 72h | 168h | 720h |
| Default | 24h | 72h | 168h | 720h |

When multiple frameworks apply to an instance, the strictest SLA wins.

### Maintenance Windows

The agent respects SSM maintenance windows:
- SCHEDULED — next window is within SLA → wait for it
- EMERGENCY — next window exceeds SLA → patch immediately

Sample windows (deployed by sample environment stack): dev daily 1AM UTC, staging Tuesday 2AM UTC, prod 1st of month 2AM UTC.

---

## Web UI

Browser-based operations console with dashboard and conversational chat.

Dashboard: KPI bar, environment status cards, vulnerability table, activity feed, compliance summary, decision explainability table.

Chat: Conversational agent with streaming responses, specialist indicators, and approval buttons for phased rollout.

### Deployed (Fargate)

The `PatchAutomationUI` stack deploys the UI as a single Fargate container behind an internal ALB. The container runs a multi-stage Docker build: Node builds the React frontend, Python serves it via FastAPI alongside the API backend.

```bash
./deploy.sh ui    # Redeploy after UI code changes
```

The ALB URL is printed after deployment and available in CloudFormation outputs. The ALB is internal (private subnets only) — access it via VPN, bastion host, or modify the stack for internet-facing if needed.

The Docker build requires `agent/.bedrock_agentcore.yaml` to exist (created by `./deploy.sh agent`). Deploy the agent before building the UI container.

SSE streaming is supported with a 300-second ALB idle timeout.

### Local Development

```bash
# Backend (port 8000)
cd ui/api && python3 server.py

# Frontend (port 5173)
cd ui/frontend && npm run dev
```

---

## CLI Chat Client

Rich terminal client for testing agent interactions without the web UI.

```bash
# Interactive mode
./chat.sh

# Single message
./chat.sh -m "What critical vulnerabilities do we have?"
```

---

## Observability

### CloudWatch Logs

```bash
# Real-time agent logs
aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-name>-DEFAULT --follow

# Recent errors
aws logs filter-log-events \
  --log-group-name "/aws/bedrock-agentcore/runtimes/<runtime-name>-DEFAULT" \
  --filter-pattern "ERROR" \
  --start-time $(date -v-1H +%s)000

# Find your runtime name
agentcore status | grep "Runtime:"
```

### Decision Audit

Every agent decision is logged to CloudWatch and S3 compliance reports in structured JSON. CloudWatch Insights query for all decisions:

```
fields @timestamp, agent, decision_type, decision, reasoning
| filter @message like /DECISION:/
| sort @timestamp desc
```

GenAI observability: `CloudWatch Console → GenAI Observability → AgentCore` for token usage, latency, and X-Ray tracing.

---

## Security

- API key authentication (opt-in via `API_KEY` env var)
- Input validation and rate limiting (20 req/min) on the web UI API
- Instance ID regex validation on all SSM operations
- IAM least privilege — write access only for SSM commands and S3 compliance reports
- Human approval gates between environments during phased rollout

---

## Troubleshooting

### Agent Not Responding
```bash
agentcore status
agentcore launch --auto-update-on-conflict    # Redeploy
```

### Permission Errors
```bash
aws cloudformation describe-stacks --stack-name PatchAutomationAgentCorePermissions
```

### No Instances Found
Instances must be SSM-managed and tagged with `Environment`:
```bash
aws ssm describe-instance-information --query 'InstanceInformationList[].InstanceId'
```

### Config Recorder Conflict
If your account already has an AWS Config recorder, deploy with:
```bash
cd infra && npx cdk deploy --all -c agentCoreRoleArn="$AGENTCORE_ROLE_ARN" -c skipConfigRecorder=true
```

### Known Limitations
- Manual approval required between environments in phased rollout
- CloudWatch alarms must be pre-configured for application-layer health checks
- Linux-only rollback (yum-based, Amazon Linux/RHEL)
- Single region deployment

---

## Usage Examples

```bash
# Via web UI
cd ui/api && python3 server.py    # Start backend
cd ui/frontend && npm run dev     # Start frontend
# Open http://localhost:5173

# Via CLI
./chat.sh
./chat.sh -m "What critical vulnerabilities do we have?"
./chat.sh -m "Handle CVE-2025-38527 in dev"
./chat.sh -m "Show SLA breaches this week"

# Direct agent invocation
cd agent && agentcore invoke '{"prompt": "Show dev instances"}'
```
