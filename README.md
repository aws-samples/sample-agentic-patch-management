# Intelligent Patch Automation

Multi-agent system for automated vulnerability-driven patch management using Strands SDK, Amazon Bedrock AgentCore, and AWS Systems Manager.

---

## Why This Matters

Traditional patching: 15 browser tabs, 3 CLI tools, 45 minutes of manual work per CVE.

This approach: natural language query → intelligent decision → automated execution.

```
You: Handle CVE-2025-38477 in dev environment
Agent: Checking maintenance window... Emergency patching required.
       Patching dev with approval gates for staging/prod.
       Complete. Compliance report saved to S3.
```

Agents make risk-based decisions using maintenance windows and CVSS scores. Application-layer health monitoring via CloudWatch alarms ensures safe patching. Built entirely on AWS native services.

> **Note:** This implementation targets Amazon Linux 2 (yum-based patching, rollback via `yum history undo`). The architecture, agent patterns, multi-agent routing, SLA enforcement, and compliance reporting are OS-agnostic. To support other operating systems (Ubuntu/Debian, Windows, RHEL, SUSE), adapt the patching and rollback commands in `agent/helper/tools.py` to use the appropriate package manager (`apt`, `zypper`, Windows Update API via SSM `AWS-RunPowerShellScript`). SSM Patch Manager and Inspector findings work across all supported OS types without changes.

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
│   Analysis   │ • Dry-Run Preview │ • SLA Tracking            │
│ • Fleet-Wide │ • Patch Execution │ • Audit Trails            │
│   Impact     │ • Health Checks   │ • Executive Reporting     │
│ • Rollout    │ • Rollback +      │ • Trend Analysis          │
│   Planning   │   Verification    │                           │
│              │ • Compliance Delta│                           │
│              │ • Maintenance     │                           │
│              │   Windows         │                           │
└─────────────┴───────────────────┴───────────────────────────┘
```

The supervisor routes queries to the right specialist automatically. If a specialist can't handle a query, the supervisor re-routes or falls back to direct AWS API access via `use_aws`.

### Patching Workflow

Every patching operation follows a mandatory workflow:

1. **Discover** affected instances and calculate SLA requirement
2. **Maintenance window** check — find next available window
3. **SLA decision**: SCHEDULED (wait for window) or EMERGENCY (patch now)
4. **Dry-run scan** — runs `AWS-RunPatchBaseline` with `Operation: Scan` (same mechanism as patching uses for Install) to show exactly which patches are missing, their severity, and classification. Operator confirms before proceeding.
5. **Capture pre-patch state** — snapshot compliance metrics (missing/installed counts) for before/after comparison. Runs after the dry-run scan so data is populated.
6. **Execute** patching via SSM (`AWS-RunPatchBaseline` with `Operation: Install`)
7. **Verify** instance health (SSM connectivity) and application health (CloudWatch alarms)
8. **Capture post-patch state** — snapshot compliance metrics after patching
9. **Generate compliance report** with before/after delta (saved to S3)

If rollback is needed, the agent runs `yum history undo` and then verifies the rollback succeeded — re-scanning with `RunPatchBaseline` to confirm patch counts returned to pre-patch levels, plus SSM health and CloudWatch alarm checks.

Decision logic: if the next maintenance window is within the SLA deadline → SCHEDULED. If the window would exceed the SLA → EMERGENCY.

---

## Prerequisites

- Python 3.11+, Node.js 18+, Docker
- AWS CLI v2 with a configured named profile
- [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) (for UI access)
- AWS account with Bedrock model access enabled (Claude Sonnet)

---

## Quick Start

```bash
# 1. Configure
cp .env.example .env
# Edit .env — set AWS_PROFILE and AWS_REGION

# 2. Enable HTTPS (optional, recommended)
./setup-tls.sh

# 3. Deploy everything (with sample EC2 instances)
./deploy.sh --with-sample-env

# 4. Connect to the UI
./connect-ui.sh
# Then open https://localhost:8443 in your browser
```

`deploy.sh` handles the full sequence: Python venv → agent → role detection → CDK bootstrap → infrastructure → Docker build → Fargate UI.

---

## Deployment

### Environment Variables

All scripts auto-source `.env` from the project root.

| Variable | Required | Description |
|----------|----------|-------------|
| `AWS_PROFILE` | Yes | AWS CLI named profile |
| `AWS_REGION` | Yes | Defaults to `us-east-1` |
| `AGENTCORE_ROLE_ARN` | No | Auto-detected after agent deploy |
| `EXISTING_VPC_ID` | No | Use an existing VPC instead of creating one. Skips NetworkingStack. |
| `ACM_CERTIFICATE_ARN` | No | Enables HTTPS. Run `./setup-tls.sh` or use your own ACM cert ARN. |
| `API_KEY` | No | Enables `X-API-Key` header auth on the web UI (operator role) |
| `API_KEY_OPERATOR` | No | Explicit operator key — full access (dashboard + chat + patching) |
| `API_KEY_VIEWER` | No | Viewer key — read-only access (dashboard + health only, no chat) |

### Commands

| Command | What it does |
|---------|-------------|
| `./deploy.sh` | Full deploy: infra + agent + UI |
| `./deploy.sh --with-sample-env` | Full deploy + 15 sample EC2 instances |
| `./deploy.sh agent` | Redeploy agent only |
| `./deploy.sh ui` | Redeploy UI only |
| `./connect-ui.sh` | SSM port forward to the internal ALB |
| `./setup-tls.sh` | Generate self-signed cert + import to ACM |
| `./deploy.sh destroy` | Destroy ALL infrastructure |
| `./deploy.sh destroy --sample-only` | Destroy sample environment only |

### Infrastructure Stacks

| Stack | Purpose |
|-------|---------|
| `Patchy-Network` | VPC, subnets, NAT gateways (skipped when `EXISTING_VPC_ID` is set) |
| `Patchy-Core` | S3 compliance reports bucket, AWS Config recorder, AgentCore IAM policy |
| `Patchy-UI` | ECS Fargate, internal ALB, CloudWatch logs |
| `Patchy-SampleEnv` | Sample environment: 15 EC2 instances, SG, IAM role, maintenance windows (deploy with `--with-sample-env`) |

The sample environment is fully self-contained — it creates its own security group, instance role, and instance profile. It deploys and destroys independently: `./deploy.sh destroy --sample-only`.

> ⚠️ **Production Warning — S3 Bucket Retention**: The compliance reports S3 bucket defaults to `DESTROY` with `autoDeleteObjects` for clean teardown. For production, change `removalPolicy` to `RETAIN` in `infra/lib/core-stack.ts` to preserve compliance reports if the stack is deleted. See [Production Hardening → Data and Compliance](#data-and-compliance) for details.

### Using an Existing VPC

If you already have a VPC with the required subnet types, set `EXISTING_VPC_ID` in `.env`:

```bash
EXISTING_VPC_ID=vpc-0123456789abcdef0
```

Requirements for the existing VPC:
- Private subnets with egress (NAT gateway) for Fargate tasks
- DNS hostnames and DNS support enabled

When set, the `Patchy-Network` stack is not created. CDK looks up the VPC at synth time using your AWS credentials.

If your account already has an AWS Config recorder:
```bash
cd infra && npx cdk deploy Patchy-Core -c agentCoreRoleArn="$AGENTCORE_ROLE_ARN" -c skipConfigRecorder=true
```

### Instance Requirements

For the agent to manage EC2 instances, they need:
- SSM Agent installed and running
- IAM instance profile with `AmazonSSMManagedInstanceCore`
- Tag: `Environment` (e.g., `dev`, `staging`, `prod`)
- Tag: `ComplianceFrameworks` (optional, e.g., `PCI-DSS,SOC2`) for SLA enforcement

The sample environment creates instances with all of these pre-configured.

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
- SCHEDULED: next window is within SLA → wait for it
- EMERGENCY: next window exceeds SLA → patch immediately

Sample windows (from `--with-sample-env`): dev daily 1AM UTC, staging Tuesday 2AM UTC, prod 1st of month 2AM UTC.

---

## Web UI

Browser-based operations console with dashboard and conversational chat.

- Dashboard: KPI bar, environment status cards, vulnerability table, compliance summary, decision explainability table
- Chat: streaming responses, specialist indicators, approval buttons for phased rollout

### Accessing the UI

The UI runs on an internal ALB (not publicly accessible). Access via SSM port forwarding:

```bash
./connect-ui.sh       # Start tunnel, then open https://localhost:8443
```

Requires the AWS CLI Session Manager plugin and at least one SSM-managed instance in the VPC.

### TLS / HTTPS

| Approach | When to use |
|----------|-------------|
| `./setup-tls.sh` | Quick start / pilot. Browser shows cert warning (self-signed). |
| ACM public certificate | Production. Set ARN in `.env`. Free, auto-renewing, no warnings. |
| No certificate | Leave `ACM_CERTIFICATE_ARN` unset. ALB serves HTTP on port 80. |

### Local Development

```bash
cd ui/api && python3 server.py        # Backend on port 8000
cd ui/frontend && npm run dev          # Frontend on port 5173
```

---

## Observability

```bash
# Real-time agent logs
aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-name>-DEFAULT --follow

# Recent errors
aws logs filter-log-events \
  --log-group-name "/aws/bedrock-agentcore/runtimes/<runtime-name>-DEFAULT" \
  --filter-pattern "ERROR" --start-time $(date -v-1H +%s)000

# Find your runtime name
agentcore status | grep "Runtime:"
```

GenAI observability: `CloudWatch Console → GenAI Observability → AgentCore` for token usage, latency, and X-Ray tracing.

Every agent decision is logged to CloudWatch and S3 compliance reports. CloudWatch Insights query:
```
fields @timestamp, agent, decision_type, decision, reasoning
| filter @message like /DECISION:/
| sort @timestamp desc
```

---

## Security

- Internal ALB (no public exposure) — accessed via SSM port forwarding
- HTTPS via ACM certificate
- API key authentication (opt-in via `API_KEY` env var)
- Role-based access control: `operator` (full access) and `viewer` (dashboard read-only, no chat/patching). Configure via `API_KEY_OPERATOR` and `API_KEY_VIEWER` env vars. Frontend auto-detects role and disables chat input for viewers.
- Rate limiting (20 req/min per client IP)
- Instance ID regex validation on all SSM operations
- IAM least privilege — write access scoped to SSM commands and S3 compliance bucket
- Human approval gates between environments during phased rollout
- Dry-run gate — programmatic enforcement requires a recent Scan before any Install operation

---

## Production Hardening

The solution ships ready for pilot use. For production workloads, consider the following:

### Authentication and Authorization

- **Corporate IdP integration**: Add a Cognito user pool with SAML/OIDC federation to your corporate IdP (Okta, Azure AD, Ping). Protect the API with Cognito authorizer tokens. The internal ALB + SSM tunnel means standard OIDC redirect flows won't work directly — use Cognito's hosted UI with token-based auth instead of ALB-native OIDC.
- **Role-based access control**: Parse JWT claims in `server.py` middleware to enforce roles (e.g., read-only viewers vs. patch operators). Restrict destructive operations (patch execution, rollback) to specific groups.
- **Per-user audit trail**: Log the authenticated user identity with each agent invocation. Currently the agent logs decisions but not who initiated them.

### Network and Access

- **AWS Client VPN**: For broader team access beyond individual SSM tunnels, deploy a Client VPN endpoint in the VPC. This gives multiple operators simultaneous access to the internal ALB without per-user port forwarding.
- **VPC endpoints**: Add interface VPC endpoints for SSM, EC2, S3, STS, and Bedrock to eliminate NAT gateway traffic and reduce data transfer costs. Also improves latency for API calls.
- **WAF**: Even on an internal ALB, AWS WAF can add request filtering, SQL injection protection, and managed rule sets as defense-in-depth.

### Data and Compliance

- **S3 bucket retention**: Change `removalPolicy` to `RETAIN` and remove `autoDeleteObjects` in `core-stack.ts` to preserve compliance reports on stack teardown. Current default is `DESTROY` for clean teardown.
- **S3 Object Lock**: Enable Object Lock (compliance mode) on the compliance reports bucket to make audit data immutable. Prevents accidental or malicious deletion of compliance evidence.
- **KMS encryption**: Replace S3-managed encryption with a customer-managed KMS key for compliance reports and Config snapshots. Enables key rotation policies and CloudTrail key usage auditing.
- **Cross-account backup**: Replicate the compliance reports bucket to a separate audit account using S3 Cross-Region Replication. Ensures reports survive even if the primary account is compromised.

### Availability and Scaling

- **Multi-AZ Fargate**: Increase `desiredCount` to 2+ in `ui-stack.ts` for high availability. The ALB already spans multiple AZs.
- **Auto-scaling**: Add ECS Service Auto Scaling based on CPU/memory utilization or request count to handle variable operator load.
- **Multi-region**: Deploy the infrastructure stack to a secondary region with Route 53 health checks for disaster recovery. The agent is stateless, so failover is straightforward.
- **ALB health check tuning**: Tighten health check thresholds for faster failover. Consider adding a deeper health check endpoint that validates downstream connectivity (SSM, Bedrock).

### Agent Reliability

- **Guardrails**: Add Bedrock Guardrails to constrain agent responses — prevent the agent from executing actions outside its intended scope (e.g., terminating instances, modifying security groups).
- **Token budget limits**: Set per-invocation token limits to prevent runaway conversations from consuming excessive Bedrock capacity.
- **Dead letter queue**: Add an SQS DLQ for failed agent invocations to capture and retry failed patch operations.
- **Idempotency**: Add idempotency keys to patch operations to prevent duplicate patching if an operator retries a failed request.
- **Rollback for Debian/Ubuntu**: Current rollback is yum-based (Amazon Linux/RHEL only). Add apt-based rollback for Debian-family instances.

### Operational Excellence

- **CI/CD pipeline**: Add a CodePipeline or GitHub Actions workflow for automated testing and deployment. Run `npx tsc --noEmit` and Python linting on PR, deploy to a staging account on merge, promote to production with manual approval.
- **Infrastructure testing**: Add CDK snapshot tests and integration tests that deploy to a test account, run agent interactions, and validate outcomes.
- **CloudWatch dashboards**: Create a pre-built CloudWatch dashboard with key metrics: agent invocation count, latency P50/P99, error rate, patch success rate, SLA compliance percentage.
- **CloudWatch alarms**: Add alarms for agent error rate spikes, Fargate task unhealthy, ALB 5xx rate, and SLA breach count. Route to SNS → PagerDuty/Slack.
- **Cost monitoring**: Tag all resources with `Project=patch-automation` and set up AWS Budgets alerts. Bedrock token usage is the primary variable cost — monitor via GenAI Observability.

### TLS

- Replace the self-signed certificate with an ACM public certificate tied to your domain (free, auto-renewing, no browser warnings). If using Client VPN, you can use a private CA certificate instead.

---

## Teardown

```bash
./deploy.sh destroy --sample-only    # Remove sample environment only
./deploy.sh destroy                # Remove ALL infrastructure (prompts for confirmation)
agentcore destroy                  # Remove the AgentCore agent runtime separately
```

S3 buckets are auto-emptied and deleted with the stack. No manual cleanup needed.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Agent not responding | `agentcore status` then `agentcore launch --auto-update-on-conflict` |
| Permission errors | Check `Patchy-Core` stack outputs |
| No instances found | Instances need SSM Agent + `Environment` tag. Check: `aws ssm describe-instance-information` |
| Config recorder conflict | Deploy with `-c skipConfigRecorder=true` |
| UI container unhealthy | Check logs: `aws logs tail /patch-automation/ui --follow` |
| SSM tunnel fails | Ensure Session Manager plugin is installed and at least one instance is online |

### Known Limitations

- Manual approval required between environments in phased rollout
- CloudWatch alarms must be pre-configured for application-layer health checks
- Linux-only patching and rollback (Amazon Linux 2 / yum-based). See note at top for adapting to other OS types.
- Single region deployment

---

## Usage Examples

Use the chat panel in the Web UI:

```
What critical vulnerabilities do we have?
How widespread is CVE-2025-38477 across our fleet?
Handle CVE-2025-38477 in dev
Run a dry-run scan on dev to preview missing patches
Show SLA breaches this week
Patch all critical CVEs in staging
```

The agent will automatically assess fleet-wide impact when scanning vulnerabilities, run a dry-run preview before patching, capture before/after compliance snapshots, and include the compliance delta in the report. If rollback is needed, it verifies the rollback succeeded before reporting back.
