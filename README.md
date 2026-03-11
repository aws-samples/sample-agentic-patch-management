# Intelligent Patch Automation for AWS — AI-Driven Vulnerability Response

Multi-agent system for automated, vulnerability-driven patch management using Strands SDK, Amazon Bedrock AgentCore, and AWS Systems Manager.

```
You: Handle CVE-2025-38477 in dev environment
Agent: Checking maintenance window... Emergency patching required.
       Patching dev with approval gates for staging/prod.
       Complete. Compliance report saved to S3.
```

Natural language in -> risk-based decision -> automated execution. No browser tabs, no manual triage.

---

## Quick Start

```bash
# 1. Configure
cp .env.example .env
# Edit .env -- set AWS_PROFILE and AWS_REGION

# 2. Deploy everything (with sample EC2 instances)
./deploy.sh --with-sample-env
# Takes ~20-30 minutes on first run (CDK bootstrap, Docker build, Fargate startup)

# 3. Create a Cognito user
POOL_ID=$(aws cloudformation describe-stacks --stack-name Patchy-UI \
  --query 'Stacks[0].Outputs[?OutputKey==`CognitoUserPoolId`].OutputValue' --output text)

aws cognito-idp admin-create-user --user-pool-id $POOL_ID \
  --username your-email@example.com \
  --user-attributes Name=email,Value=your-email@example.com Name=email_verified,Value=true \
  --temporary-password 'TempPass1!' --message-action SUPPRESS

# Set a permanent password (bypasses Cognito's force-change-on-first-login flow)
aws cognito-idp admin-set-user-password --user-pool-id $POOL_ID \
  --username your-email@example.com --password 'YourSecurePass1!' --permanent

# Grant operator access (full dashboard + chat + patching)
aws cognito-idp admin-add-user-to-group --user-pool-id $POOL_ID \
  --username your-email@example.com --group-name operators

# 4. Verify deployment
UI_URL=$(aws cloudformation describe-stacks --stack-name Patchy-UI \
  --query 'Stacks[0].Outputs[?OutputKey==`UIUrl`].OutputValue' --output text)
echo "UI URL: $UI_URL"
curl -sk "$UI_URL/api/health"   # Should return: {"status":"ok"}

# 5. Open the URL in your browser and log in
#    (You will see a self-signed cert warning -- click through. This is expected.)
```

`deploy.sh` handles the full sequence automatically: Python venv -> agent deployment -> role detection -> CDK bootstrap -> infrastructure (4 stacks) -> TLS certificate -> Cognito -> Docker build -> Fargate deployment.

---

## What It Looks Like

```
You: What critical vulnerabilities do we have in dev?

Agent: [Consulting Vulnerability Analyst]

      CRITICAL  2 vulnerabilities across 5 instances in dev

      CRITICAL: 2 | HIGH: 0 | MEDIUM: 0 | LOW: 0

      | CVE ID            | CVSS | Severity | Instances | Fix Available |
      |-------------------|------|----------|-----------|---------------|
      | CVE-2025-38477    | 9.8  | CRITICAL | 5         | YES           |
      | CVE-2025-21001    | 9.1  | CRITICAL | 3         | YES           |

      Fleet Impact (CVE-2025-38477):
      | Environment | Instances | SLA (tag) | Next Window    | Hours Until |
      |-------------|-----------|-----------|----------------|-------------|
      | dev         | 5         | 24hr      | dev-daily 1AM  | 6hr         |
      | staging     | 3         | 24hr      | staging Tue 2AM| 52hr        |

      Recommended rollout: dev -> staging

      ## Next Steps
      1. Preview patches for dev -- 5 instances, 2 critical CVEs
      2. Check if prod is also affected
      3. Build remediation plan across dev and staging
```

```
You: Preview patches for dev

Agent: [Consulting Patch Manager]

      SLA: 24hr (tag:SLA-CRITICAL) | Window: 6hr | Decision: SCHEDULED

      Dry-run complete -- 12 missing patches across 5 instances

      | Instance ID         | Missing | Installed | Failed | Status  |
      |---------------------|---------|-----------|--------|---------|
      | i-0a1b2c3d4e5f67890 | 3       | 145       | 0      | Success |
      | i-0b2c3d4e5f678901a | 2       | 148       | 0      | Success |
      | i-0c3d4e5f6789012ab | 3       | 141       | 0      | Success |
      | i-0d4e5f67890123abc | 2       | 150       | 0      | Success |
      | i-0e5f678901234abcd | 2       | 147       | 0      | Success |

      Review the changes above. Say 'proceed' to execute patching or 'cancel'.

      ## Next Steps
      1. Start patching dev -- 5 instances (CRITICAL CVEs)
      2. Generate compliance report for dev
      3. Check if staging is also affected
```

More example prompts:

```
How widespread is CVE-2025-38477 across our fleet?
Patch all critical CVEs in staging
Run a dry-run scan on prod to preview missing patches
Show SLA breaches this week
Roll back dev patches
Who patched prod last week?
Generate a compliance report for PCI-DSS in staging
```

---

## Known Limitations

Read these before deciding to adopt -- they define the current scope:

- **Amazon Linux 2 only** for patching and rollback. Rollback uses `yum history undo` which does not work on Ubuntu/Debian/Windows. SSM Patch Manager and Inspector are OS-agnostic -- only the rollback command needs adaptation for other OS types. See [Extending the Solution](docs/extending.md).
- **Single AWS account and region.** Multi-account/region extension is planned.
- **Severity-level patching, not per-CVE.** SSM Patch Manager cannot install a single CVE's patch in isolation. The agent scopes to severity level (e.g., all CRITICAL patches) using BaselineOverride files.
- **Manual approval required** between environments in staged rollout. The agent asks the operator before proceeding to the next environment.
- **Self-signed TLS certificate** by default. Browser shows a warning. Replace with an ACM public certificate for production. See [Security](docs/security.md).
- **CloudWatch alarms must be pre-configured** for application-layer health checks. The agent checks existing alarms but does not create them.
- **Baseline overrides are Amazon Linux 2 only.** Other OS types need equivalent override JSON files.

---

## Architecture

```
                                  +---------------------------+
                                  |       Web Browser         |
                                  +-------------+-------------+
                                                |
                                  +-------------v-------------+
                                  |     ALB (Cognito Auth)    |
                                  +-------------+-------------+
                                                |
                        +----------------------------------------------+
                        |              ECS Fargate                     |
                        |   React Frontend + FastAPI API Proxy         |
                        +---------------------+------------------------+
                                              |
                                    SSE streaming via
                                  invoke_agent_runtime
                                              |
                        +---------------------v------------------------+
                        |         Amazon Bedrock AgentCore             |
                        |      (Runtime + Memory STM/LTM + OTEL)      |
                        +---------------------+------------------------+
                                              |
              +-------------------------------+-------------------------------+
              |                               |                               |
+-------------v-----------+   +---------------v-----------+   +---------------v-----------+
| Supervisor Agent        |   |                           |   |                           |
| - Routes queries        |   |  Amazon Inspector         |   |  AWS Systems Manager      |
| - Enriches context      |   |  (Vulnerability findings) |   |  (Patch, commands, windows)|
| - Memory read/write     |   |                           |   |                           |
+---+--------+--------+--+   +---------------------------+   +---------------------------+
    |        |        |
    v        v        v                                       +---------------------------+
 Vuln     Patch   Compliance                                  |  Amazon S3                |
 Analyst  Manager  Analyst                                    |  (Compliance reports,     |
                                                              |   baseline overrides,     |
                                                              |   decision audit trail)   |
                                                              +---------------------------+
```

**Agent hierarchy:**

| Agent | Role | Memory | Tools |
|-------|------|--------|-------|
| Supervisor | Routes queries, enriches context, manages conversation | Read/Write (STM + LTM) | `consult_vulnerability_analyst`, `consult_patch_manager`, `consult_compliance_analyst`, `use_aws` |
| Vulnerability Analyst | CVE analysis, fleet impact assessment | Read-only | `get_vulnerability_findings`, `assess_fleet_impact` |
| Patch Manager | SLA enforcement, dry-run, patching, rollback, compliance reports | Read-only | 14 tools covering full lifecycle |
| Compliance Analyst | S3 report queries, SLA breach tracking, trend analysis | Read-only | `query_compliance_reports` |

**Patch execution workflow** (enforced sequence -- the agent will not skip steps):

| Step | What happens |
|------|-------------|
| 1. Discover | Get patch compliance state and SLA requirement for target instances |
| 2. Check windows + policy | Find next SSM maintenance window; check for existing Patch Policy associations |
| 3. SLA decision | Window within SLA -> SCHEDULED. Window exceeds SLA -> EMERGENCY (patch now) |
| 4. Dry-run scan | `AWS-RunPatchBaseline` with `Operation: Scan`. Operator reviews before proceeding. **Code-enforced: no Install without a Scan within the last 2 hours** |
| 5. Pre-patch snapshot | Capture compliance metrics per instance for before/after comparison |
| 6. Execute | `AWS-RunPatchBaseline` with `Operation: Install` |
| 7. Health check | Verify SSM connectivity and CloudWatch alarms |
| 8. Post-patch snapshot | Capture compliance metrics after patching |
| 9. CVE verification | Query Inspector to confirm CVEs are remediated |
| 10. Compliance report | JSON report with before/after delta, SLA result, operator identity -> S3 |

For detailed architecture documentation (memory model, SLA decision flow, severity-scoped patching, rollback verification, patch policy integration), see [Architecture](docs/architecture.md).

**Key files:**

| File | Purpose |
|------|---------|
| `agent/supervisor.py` | Routing logic, context enrichment, memory integration |
| `agent/vulnerability_agent.py` | CVE analysis, fleet impact via Inspector |
| `agent/patch_manager.py` | SLA enforcement, dry-run, patching, rollback |
| `agent/compliance_analyst.py` | S3 report queries, SLA breach tracking |
| `agent/helper/tools.py` | All AWS API wrappers (17 tools) |
| `agent/helper/agent_factory.py` | Agent creation with memory + guardrails |
| `ui/api/server.py` | FastAPI proxy, dashboard API, auth middleware |
| `ui/frontend/src/App.tsx` | React UI -- dashboard + chat panels |
| `infra/lib/` | CDK stacks (Network, Core, UI, SampleEnv) |

---

## Prerequisites

**Tools:**
- Python 3.11+, Node.js 18+, Docker
- AWS CLI v2 with a configured named profile
- [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) (for `connect-ui.sh`, only needed if using internal ALB mode)
- Amazon Bedrock AgentCore access in your target region

**AWS services -- must be enabled before deploying:**

| Service | Purpose | How to verify |
|---------|---------|---------------|
| Amazon Bedrock | LLM for agent reasoning | Console -> Bedrock -> Model access -> Claude Sonnet enabled |
| Amazon Bedrock AgentCore | Agent hosting, memory, observability | Available in your target region |
| AWS Systems Manager | Patch management, command execution | `aws ssm describe-instance-information` returns instances |
| Amazon Inspector | Vulnerability scanning (CVE discovery) | Console -> Inspector -> enabled for EC2 scanning |
| Amazon S3 | Compliance reports storage | Auto-created by `Patchy-Core` stack |

**Estimated monthly cost (pilot):**

| Component | Approximate Cost | Notes |
|-----------|-----------------|-------|
| NAT Gateways (2) | ~$64/month | Required for private subnet egress |
| ECS Fargate (1 task, 256 CPU / 512 MB) | ~$10/month | UI container |
| Application Load Balancer | ~$16/month + data transfer | Public or internal |
| Bedrock (Claude Sonnet) | Variable | ~$3/1M input tokens, ~$15/1M output tokens |
| Inspector | Per-instance | ~$1.25/instance/month for EC2 scanning |
| Sample EC2 instances (15x t3.micro) | ~$75/month | Only if using `--with-sample-env` |
| **Total (pilot with sample env)** | **~$170-200/month** | Bedrock usage depends on conversation volume |

Costs scale with fleet size (Inspector) and usage (Bedrock tokens). Monitor via AWS Cost Explorer with `Project=patch-automation` tag.

### EC2 Instance Requirements

For the agent to manage your EC2 instances, they need:

**Required:**
- SSM Agent installed and running (`aws ssm describe-instance-information` shows `Online`)
- IAM instance profile with `AmazonSSMManagedInstanceCore` managed policy
- Tag: `Environment` (e.g., `dev`, `staging`, `prod`) -- used for environment-based routing
- Tag: `PatchGroup` (e.g., `dev-patch-group`) -- used by SSM Patch Manager

**Recommended:**
- Tags: `SLA-CRITICAL`, `SLA-HIGH`, `SLA-MEDIUM`, `SLA-LOW` (hours) -- drives SLA enforcement. Falls back to defaults (24/72/168/720h) if missing.
- IAM instance profile allows `s3:GetObject` on `s3://patch-compliance-reports-<account-id>/baseline-overrides/*` for severity-scoped patching (the sample environment configures this automatically)

**Optional (audit context):**
- Tag: `ComplianceFrameworks` (e.g., `PCI-DSS,SOC2`) -- included in compliance reports
- Tags: `Team`, `Product`, `Owner` -- included in compliance reports for attribution

---

## Deployment

### Required Environment Variables

All scripts auto-source `.env` from the project root. Copy `.env.example` to get started.

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_PROFILE` | `default` | AWS CLI named profile |
| `AWS_REGION` | `us-east-1` | Target AWS region |

### Optional Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `EXISTING_VPC_ID` | (none) | Use your own VPC instead of creating one |
| `ACM_CERTIFICATE_ARN` | (auto-generated) | TLS certificate. Auto-created via `setup-tls.sh` if not set |
| `COGNITO_ENABLED` | `true` | Set to `false` for internal ALB mode (no public access) |
| `COGNITO_DOMAIN_PREFIX` | `patchy-<account_id>` | Cognito hosted UI domain prefix |
| `AGENTCORE_ROLE_ARN` | (auto-detected) | Only set manually if multiple AgentCore roles exist |
| `BYPASS_TOOL_CONSENT` | `true` | Strands SDK tool consent. When `true`, the agent executes tools without interactive CLI prompts. **This does not bypass operator approval** -- the agent's workflow still requires human confirmation before patching (enforced by system prompt and the code-level dry-run gate). |
| `INSPECTOR_RESOURCE_TYPES` | `EC2` | Comma-separated: `EC2`, `ECR`, `LAMBDA`. Set to `EC2,ECR` for container workloads. |
| `API_KEY` / `API_KEY_OPERATOR` / `API_KEY_VIEWER` | (none) | Header-based auth (only when `COGNITO_ENABLED=false`) |
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-sonnet-4-6` | Override the default model |

### Commands

| Command | What it does | Time |
|---------|-------------|------|
| `./deploy.sh` | Full deploy: agent + infra + UI | ~20-30 min (first run) |
| `./deploy.sh --with-sample-env` | Full deploy + 15 sample EC2 instances | ~25-35 min (first run) |
| `./deploy.sh agent` | Redeploy agent only (after code changes) | ~5 min |
| `./deploy.sh ui` | Redeploy UI only (after code changes) | ~10 min |
| `./connect-ui.sh` | SSM port forward to internal ALB | Instant |
| `./deploy.sh destroy` | Destroy ALL infrastructure | ~10 min |
| `./deploy.sh destroy --sample-only` | Destroy sample environment only | ~5 min |

### Infrastructure Stacks

| Stack | Purpose | Always Deployed? |
|-------|---------|------------------|
| `Patchy-Network` | VPC, subnets, NAT gateways, flow logs | Yes (or `Patchy-VpcLookup` if `EXISTING_VPC_ID` set) |
| `Patchy-Core` | S3 compliance reports bucket, AgentCore IAM policy | Yes |
| `Patchy-UI` | ECS Fargate, ALB, Cognito, bastion (internal mode) | Yes |
| `Patchy-SampleEnv` | 15 EC2 instances, maintenance windows, ALBs, patch baselines | Only with `--with-sample-env` |

> [!WARNING]
> The compliance reports S3 bucket defaults to `DESTROY` with `autoDeleteObjects` for clean teardown. **For production, change `removalPolicy` to `RETAIN` in `infra/lib/core-stack.ts`** to preserve audit data if the stack is deleted.

### Using an Existing VPC

Set `EXISTING_VPC_ID` in `.env` to skip VPC creation:

```bash
EXISTING_VPC_ID=vpc-0123456789abcdef0
```

Requirements: private subnets with egress (NAT gateway), DNS hostnames and DNS support enabled.

### Tagging Convention

The agent reads EC2 tags to determine environment, SLA, and compliance context. See [EC2 Instance Requirements](#ec2-instance-requirements) above for the full list. Most organisations already maintain this metadata in their CMDB (ServiceNow, Device42) -- if your CMDB drives EC2 tagging, the agent picks up your existing taxonomy automatically.

---

## Security

### Network Architecture

| Component | Exposure | Access Control |
|-----------|----------|----------------|
| ALB | Internet-facing (Cognito mode) or internal (bastion mode) | Cognito authentication or SSM port forwarding |
| Fargate tasks | Private subnets only | ALB -> port 8000 only |
| EC2 instances (sample env) | Private subnets only | SSM Agent (no SSH) |
| S3 compliance bucket | Private | IAM policy (AgentCore role + Fargate task role) |

### Authentication and Authorization

| Mode | How it works | When to use |
|------|-------------|-------------|
| **Cognito** (default) | ALB authenticates via Cognito hosted UI. Two groups: `operators` (full access) and `viewers` (read-only dashboard). | Production / pilot |
| **Internal ALB** (`COGNITO_ENABLED=false`) | No public access. Connect via `./connect-ui.sh` (SSM port forwarding). Optional API key auth via `X-API-Key` header. | VPN-only environments |

### IAM Permissions

The AgentCore runtime role (`PatchyAgentCorePolicy` in `Patchy-Core` stack) has:
- **EC2**: Read-only (`Describe*`)
- **SSM**: Read + `SendCommand` + maintenance window management (scoped to `resources: ['*']` -- see note below)
- **Inspector**: Read-only (`ListFindings`)
- **S3**: Read/Write on the compliance reports bucket only
- **CloudWatch**: Read-only (`DescribeAlarms`, `GetMetricData`)

> [!NOTE]
> SSM `SendCommand` is granted on `resources: ['*']`, meaning the agent can target any instance in the account. To restrict the blast radius, add a condition key scoping to specific tags (e.g., `ssm:resourceTag/ManagedBy: IntelligentPatchAutomation`). See [Security documentation](docs/security.md) for examples.

### Data Flow

- **Within your account**: All tool calls (SSM, Inspector, EC2, S3, CloudWatch) stay within your AWS account and region.
- **Bedrock API**: Agent prompts and responses are sent to the Bedrock API endpoint in your region. Bedrock does not store or train on your data. See [Bedrock data privacy](https://docs.aws.amazon.com/bedrock/latest/userguide/data-protection.html).
- **No external services**: The solution does not call any third-party APIs.

### Safety Mechanisms

- **Dry-run gate** (code-enforced): `execute_patch_operation` refuses to run without a `Scan` within the last 2 hours on every target instance. This cannot be bypassed by prompt manipulation.
- **Operator confirmation**: The agent's system prompt requires presenting dry-run results and waiting for operator approval before patching. This is prompt-enforced, not code-enforced.
- **Rate limiting**: 20 requests/minute per client IP on the API proxy.
- **Input validation**: Instance ID regex validation on all SSM operations. Session ID format validation. Message length limits (10,000 chars).
- **Per-user audit trail**: Operator identity (Cognito email) recorded in S3 reports, SSM command comments, and CloudWatch logs.

For production hardening (IdP federation, WAF, KMS encryption, S3 Object Lock, VPC endpoints), see [Security documentation](docs/security.md).

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Agent not responding | `agentcore status` then `agentcore launch --auto-update-on-conflict` |
| Permission errors | Check `Patchy-Core` stack outputs for policy ARN |
| No instances found | Instances need SSM Agent + `Environment` tag. Verify: `aws ssm describe-instance-information` |
| UI container unhealthy | Check logs: `aws logs tail /patch-automation/ui --follow` |
| SSM tunnel fails | Ensure Session Manager plugin is installed and at least one instance is `Online` |
| Cognito login redirect error | Redeploy: `./deploy.sh ui` (fixes callback URL case mismatch) |
| Cognito users gone after redeploy | User Pool was recreated (new Pool ID). Recreate users -- see [Operations](docs/operations.md) |
| Self-signed cert warning | Expected with default setup. Click through. For production, use ACM public certificate |
| Agent asks for info already provided | Memory or session issue. Verify `session_id` is consistent and memory is active: `agentcore status` |
| Stale config after account switch | Delete `agent/.bedrock_agentcore.yaml` and redeploy: `./deploy.sh agent` |

### Deploying to a New Account

1. Update `.env` with new `AWS_PROFILE` (and optionally `AWS_REGION`)
2. Delete `agent/.bedrock_agentcore.yaml` (contains old account's agent ARN and memory ID)
3. Run `./deploy.sh --with-sample-env`

The deploy script auto-detects stale configs, upgrades memory mode, configures LTM strategies, and fixes IAM permissions.

---

## Detailed Documentation

| Document | What it covers |
|----------|---------------|
| [Architecture](docs/architecture.md) | Agent hierarchy, memory model, SLA decision flow, severity-scoped patching, rollback verification, patch policy integration, compliance reporting |
| [Operations](docs/operations.md) | User management, long-term memory, guardrails, SLA configuration, maintenance windows, web UI access, local development |
| [Observability](docs/observability.md) | 4-layer observability model, GenAI dashboard, tool log queries, full conversation replay, SSM/S3 ground truth, debugging workflows |
| [Security](docs/security.md) | Production hardening, IdP federation, IAM session tags, WAF, KMS, S3 Object Lock, VPC endpoints, network architecture |
| [Extending the Solution](docs/extending.md) | Adding tools, multi-OS support, CMDB integration, change management, third-party scanners, Slack/Teams, cross-account |

---

## Teardown

```bash
./deploy.sh destroy --sample-only    # Remove sample environment only
./deploy.sh destroy                  # Remove ALL infrastructure (prompts for confirmation)
agentcore destroy                    # Remove the AgentCore agent runtime separately
```

> [!NOTE]
> `agentcore destroy` removes the agent runtime but leaves the memory resource intact (may contain audit-relevant history). To delete memory: `aws bedrock-agentcore-control delete-memory --memory-id <memory-id>`.

---

## Getting Help

- **Issues**: Report bugs or request features on the project's issue tracker
- **Logs**: Start with the [Observability guide](docs/observability.md) -- Layer 1 (GenAI Dashboard) answers most "what happened?" questions
- **Deployment issues**: Check [Troubleshooting](#troubleshooting) above, then review `deploy.sh` output for error messages
- **Security questions**: See [Security documentation](docs/security.md) for IAM scoping, network architecture, and hardening
