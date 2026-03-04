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

## What the Solution Does

### Vulnerability Discovery and Fleet-Wide Impact Assessment

The agent queries Amazon Inspector for active vulnerability findings across your fleet. When a CVE is identified, it doesn't just report it — it assesses fleet-wide impact by scanning all environments (dev, test, staging, prod) to determine how many instances are affected, which teams own them, and what compliance frameworks govern their SLA.

- Query by CVE ID, severity, or environment
- CVSS score correlation with Inspector findings
- Instance-level affected resource mapping
- Cross-environment exposure analysis ("CVE-2025-38477 affects 4 instances in dev, 3 in staging, 0 in prod")

> **Enterprise integration**: The agent queries Amazon Inspector natively. If your organisation uses third-party vulnerability scanners (Qualys, Tenable, Rapid7), you can either feed their findings into Inspector via the [Inspector SBOM integration](https://docs.aws.amazon.com/inspector/latest/user/sbom-generator.html), or extend the vulnerability analyst's tool set in `agent/helper/tools.py` to query your scanner's API directly. The multi-agent architecture means adding a new data source is a single tool function — no changes to routing or decision logic.

### Intelligent Patch Execution

Every patching operation follows a mandatory safety workflow — the agent enforces this sequence and will not skip steps:

1. **Discover** affected instances and calculate SLA requirement based on per-instance SLA tags and vulnerability severity
2. **Maintenance window + patch policy check** — find the next available SSM maintenance window for the target environment AND check whether SSM Patch Policy associations (State Manager) exist for the target instances
3. **SLA decision**: if the next window is within the SLA deadline → SCHEDULED (register with window). If the window would exceed the SLA → EMERGENCY (patch immediately)
4. **Dry-run scan** — runs `AWS-RunPatchBaseline` with `Operation: Scan` to show exactly which patches are missing, their severity, and classification. When a severity filter is specified (e.g., "patch HIGH CVEs"), the scan uses a `BaselineOverride` scoped to that severity level. The operator reviews before proceeding. This is programmatically enforced — the agent cannot execute a patch Install without a recent Scan (within 2 hours) on every target instance
5. **Capture pre-patch state** — snapshot compliance metrics (missing/installed counts per instance) for before/after comparison
6. **Execute** patching via SSM (`AWS-RunPatchBaseline` with `Operation: Install`)
7. **Verify** instance health (SSM connectivity) and application health (CloudWatch alarms)
8. **Capture post-patch state** — snapshot compliance metrics after patching
9. **Generate compliance report** with before/after delta, SLA assessment, and operator identity (saved to S3)

### Patch Policy Integration

The agent checks for existing SSM Patch Policy associations (created via Quick Setup or State Manager) on target instances. This determines the execution path:

| Scenario | Agent behavior |
|----------|---------------|
| Scheduled + no severity filter + Install policy exists | Reports: "Your patch policy will handle this during the next window. No action needed." |
| Scheduled + no severity filter + no Install policy | Registers a task with the maintenance window |
| Scheduled + severity filter | Registers a task with `BaselineOverride` (policy baseline may not match requested severity) |
| Emergency + any | Direct `send_command` with `BaselineOverride` |

If no patch policy exists for an instance, the agent warns the operator: "No patch policy found. Consider creating one for consistent scheduled patching."

### Severity-Scoped Patching

SSM Patch Manager applies patches based on patch baseline approval rules, not individual CVE IDs. When a user requests patching for a specific CVE or severity level, the agent uses `BaselineOverride` — an S3-hosted JSON file that overrides the default baseline rules at runtime — to scope the operation to the requested severity.

Pre-generated baseline override files are stored in the compliance reports S3 bucket:
- `baseline-overrides/critical-only.json` — Critical severity only
- `baseline-overrides/high-and-above.json` — Critical + Important (High)
- `baseline-overrides/medium-and-above.json` — Critical + Important + Medium
- `baseline-overrides/all-severities.json` — All severity levels

When a user says "Patch CVE-2025-1234 in prod" (a HIGH severity CVE), the agent:
1. Identifies the CVE's severity (HIGH)
2. Selects the `high-and-above` baseline override
3. Runs a dry-run scan with that override — shows only HIGH+ patches
4. Tells the user: "To remediate CVE-2025-1234, I'll apply the HIGH+ severity baseline. This will install 12 patches including your target CVE. Here's the full list."
5. User confirms → execute with the same override

> **Limitation**: SSM cannot install a single CVE's patch in isolation. The `BaselineOverride` approach scopes to severity level, not individual packages. For surgical per-CVE patching, a future enhancement could use the `InstallOverrideList` parameter with CVE → package name mapping from Inspector findings.

> **OS support**: The pre-generated baseline overrides target Amazon Linux 2. For other operating systems (Ubuntu, RHEL, Windows), create equivalent override files with the appropriate `OperatingSystem` field. The override JSON schema is documented in the [AWS Systems Manager User Guide](https://docs.aws.amazon.com/systems-manager/latest/userguide/patch-manager-about-baselineoverride.html).

### Patch Impact Preview

Before any patch operation, the agent runs a dry-run scan that shows the full impact of the operation — not just the target CVE, but every package that will be updated. This is the "blast radius" of the patch:

- **Per-CVE patching**: "You asked to patch CVE-2025-1234 (HIGH). The HIGH+ severity baseline will install 12 patches total — here's the full list with package names, severity, and CVE IDs for each."
- **Per-severity patching**: "Applying all CRITICAL patches will update 3 packages across 5 instances."
- **Full patching**: "The default baseline will install 47 patches. 5 are security-critical, 12 are important, 30 are medium."

The dry-run results include `cve_ids` for each patch, so the agent can confirm whether the target CVE is included in the patch set. If the target CVE's fix isn't available in the OS repos (common for kernel-level CVEs on Amazon Linux 2), the agent explains this.

### Rollback and Verification

If a patch causes issues, the agent runs `yum history undo` to reverse the last transaction, then verifies the rollback succeeded by:
- Re-scanning with `AWS-RunPatchBaseline` to confirm patch counts returned to pre-patch levels
- Checking SSM agent connectivity on all target instances
- Validating CloudWatch alarms returned to OK state

The agent won't report a rollback as successful until all three checks pass.

### Compliance Reporting and SLA Enforcement

Every patch operation produces a structured JSON compliance report stored in S3 with date-partitioned keys (`YYYY/MM/DD/report-id.json`). Reports include:

- Vulnerability details (CVE ID, severity, CVSS score)
- Scope (environment, team, product, instance count)
- SLA assessment (framework, required hours, actual time-to-patch, met/breached)
- Execution details (decision type, command ID, success/failure counts)
- Before/after compliance delta (missing patches reduced from X to Y, compliance improved from N% to M%)
- Operator identity (who initiated the patch operation)

The compliance analyst agent can query these reports for trend analysis, SLA breach history, and executive summaries broken down by severity, environment, or team.

> **Enterprise integration**: The S3 compliance reports are structured JSON with consistent schema — they can feed directly into your existing GRC platform (Archer, Drata, Vanta), SIEM (Splunk, Datadog), or BI tools (QuickSight, Tableau) via S3 event notifications, Kinesis Firehose, or scheduled Athena queries. If your organisation already has a compliance data lake, point the agent's S3 bucket at it or add S3 replication to your central audit account.

### Per-User Audit Trail

Every patch operation records who initiated it. The operator's email from the Cognito JWT is injected into the agent payload and recorded in compliance reports, SSM command Comments, S3 object metadata, and CloudWatch logs.

The audit trail is queryable from multiple surfaces:
- S3 compliance reports (JSON `operator` field + S3 object metadata)
- SSM command history (`Comment` field includes operator identity)
- CloudWatch Logs (`PATCH_SCHEDULED:` / `PATCH_EXECUTED:` log entries include operator)
- Web UI Decision Audit Trail table (Operator column)

### Role-Based Access Control

Two Cognito groups control what users can do:

| Role | Dashboard | Chat | Patch Execution | Reports |
|------|-----------|------|-----------------|---------|
| **operators** | ✅ | ✅ | ✅ | ✅ |
| **viewers** | ✅ | ❌ | ❌ | ✅ |

Users are assigned to groups via the Cognito CLI or console. The ALB injects the Cognito JWT into request headers, and the backend resolves the user's group membership to determine their role. See [User Management](#user-management) for setup instructions.

> **Enterprise integration**: If your organisation uses AWS IAM Identity Center (SSO) with permission sets and groups, those groups map directly to the operator/viewer roles. Federate Cognito with your Identity Center instance — group membership flows through as JWT claims, and the backend resolves roles without any custom user management.

### Web UI and Operations Console

Browser-based console with two panels:

- **Dashboard**: KPI bar (total instances, vulnerabilities, SLA compliance rate), environment status cards, vulnerability table with pagination, compliance summary by severity/environment/team, decision explainability table with operator attribution
- **Chat**: Streaming agent responses with specialist routing indicators, approval buttons for phased rollout between environments, session persistence via localStorage

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                 Patch Automation Supervisor                  │
│         (Intelligent routing + memory + fallback)           │
│         AgentCoreMemorySessionManager (STM + LTM)           │
├──────────────┬───────────────────┬───────────────────────────┤
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
└──────────────┴───────────────────┴───────────────────────────┘
  (Read-only memory)  (Read-only memory)  (Read-only memory)
```

The supervisor routes queries to the right specialist automatically. If a specialist can't handle a query, the supervisor re-routes or falls back to direct AWS API access via `use_aws`.

---

## Prerequisites

- Python 3.11+, Node.js 18+, Docker
- AWS CLI v2 with a configured named profile
- [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) (for UI access)
- AWS account with Bedrock model access enabled (Claude Sonnet)

### AWS Service Requirements

The following AWS services must be enabled and configured in your account for the solution to work:

| Service | Required | Purpose | How to verify |
|---------|----------|---------|---------------|
| Amazon Bedrock | Yes | LLM for agent reasoning | Console → Bedrock → Model access → Claude Sonnet enabled |
| AWS Systems Manager | Yes | Patch management, command execution, maintenance windows | `aws ssm describe-instance-information` returns your instances |
| Amazon Inspector | Yes | Vulnerability scanning (CVE discovery) | Console → Inspector → enabled for EC2 scanning |
| Amazon S3 | Yes | Compliance reports storage | Auto-created by `Patchy-Core` stack |

### Instance Requirements

For the agent to manage EC2 instances, they need:
- SSM Agent installed and running (`aws ssm describe-instance-information` shows the instance as `Online`)
- IAM instance profile with `AmazonSSMManagedInstanceCore` managed policy
- IAM instance profile must also allow `s3:GetObject` on `s3://patch-compliance-reports-<account-id>/baseline-overrides/*` for severity-scoped patching (the sample environment configures this automatically)
- Tag: `Environment` (e.g., `dev`, `staging`, `prod`) — used for environment-based routing
- Tag: `PatchGroup` (e.g., `dev-patch-group`) — used by SSM Patch Manager for baseline mapping
- Tag: `ComplianceFrameworks` (optional, e.g., `PCI-DSS,SOC2`) — included in compliance reports for audit context
- Tags: `SLA-CRITICAL`, `SLA-HIGH`, `SLA-MEDIUM`, `SLA-LOW` (recommended, hours) — drives SLA enforcement. Falls back to defaults (24/72/168/720h) if missing.

### Patch Policy Setup (Recommended)

For scheduled patching, the agent checks for existing SSM Patch Policy associations (State Manager). If your instances don't have patch policies configured:
- The agent will warn: "No patch policy found for these instances"
- On-demand patching still works via direct `send_command`
- To set up patch policies: Console → Systems Manager → Quick Setup → Patch Policy, or create State Manager associations targeting your instances with `AWS-RunPatchBaseline`

The sample environment (`--with-sample-env`) creates maintenance windows but not patch policy associations. In production, configure patch policies via [Quick Setup](https://docs.aws.amazon.com/systems-manager/latest/userguide/quick-setup-patch-manager.html) for consistent scheduled patching.

---

## Quick Start

```bash
# 1. Configure
cp .env.example .env
# Edit .env — set AWS_PROFILE and AWS_REGION

# 2. Deploy everything (with sample EC2 instances)
./deploy.sh --with-sample-env

# 3. Create Cognito users (URL and Pool ID are printed at the end of deploy)
POOL_ID=$(aws cloudformation describe-stacks --stack-name Patchy-UI \
  --query 'Stacks[0].Outputs[?OutputKey==`CognitoUserPoolId`].OutputValue' --output text)

# Create an operator user
aws cognito-idp admin-create-user --user-pool-id $POOL_ID \
  --username operator@patchy.com \
  --user-attributes Name=email,Value=operator@patchy.com Name=email_verified,Value=true \
  --temporary-password 'TempPass1' --message-action SUPPRESS

aws cognito-idp admin-set-user-password --user-pool-id $POOL_ID \
  --username operator@patchy.com --password 'YourPassword1' --permanent

aws cognito-idp admin-add-user-to-group --user-pool-id $POOL_ID \
  --username operator@patchy.com --group-name operators

# 4. Open the URL printed at the end of deploy and log in
```

`deploy.sh` handles the full sequence: Python venv → agent → role detection → CDK bootstrap → infrastructure → TLS certificate (auto-generated) → Cognito user pool → Docker build → Fargate UI.

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
| `ACM_CERTIFICATE_ARN` | No | Enables HTTPS. Auto-generated by `deploy.sh` via `setup-tls.sh` if not set. |
| `COGNITO_DOMAIN_PREFIX` | No | Cognito hosted UI domain prefix. Auto-derived as `patchy-<account_id>` if not set. |
| `COGNITO_ENABLED` | No | Set to `false` to disable Cognito and use internal ALB. Defaults to `true`. |
| `API_KEY` | No | Enables `X-API-Key` header auth (only relevant when `COGNITO_ENABLED=false`) |
| `API_KEY_OPERATOR` | No | Explicit operator key — full access (only relevant when `COGNITO_ENABLED=false`) |
| `API_KEY_VIEWER` | No | Viewer key — read-only (only relevant when `COGNITO_ENABLED=false`) |

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
| `Patchy-Core` | S3 compliance reports bucket, AgentCore IAM policy |
| `Patchy-UI` | ECS Fargate, ALB (public with Cognito or internal), Cognito User Pool, CloudWatch logs |
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

### Tagging Convention

The agent reads EC2 tags to determine environment, compliance frameworks, and patch group membership. See [Prerequisites → Instance Requirements](#instance-requirements) for the full list.

> **Enterprise integration**: Most organisations already maintain environment and compliance metadata in their CMDB (ServiceNow, Device42, Jira Assets). If your CMDB drives EC2 tagging through AWS Organizations tag policies, Service Catalog, or a CI/CD pipeline, the agent picks up your existing taxonomy automatically — no manual tag mapping needed.

---

## Post-Deployment Setup

The deploy script handles most setup automatically. These sections describe what happens behind the scenes and optional advanced configuration.

### User Management

The UI uses Cognito for authentication. After deployment, create users and assign them to groups.

**Get the User Pool ID:**
```bash
POOL_ID=$(aws cloudformation describe-stacks --stack-name Patchy-UI \
  --query 'Stacks[0].Outputs[?OutputKey==`CognitoUserPoolId`].OutputValue' --output text)
```

**Create a user:**
```bash
aws cognito-idp admin-create-user --user-pool-id $POOL_ID \
  --username user@example.com \
  --user-attributes Name=email,Value=user@example.com Name=email_verified,Value=true \
  --temporary-password 'TempPass1' --message-action SUPPRESS

# Set a permanent password (skips the forced change on first login)
aws cognito-idp admin-set-user-password --user-pool-id $POOL_ID \
  --username user@example.com --password 'YourSecurePassword1' --permanent
```

**Assign to a group:**
```bash
# Full access (dashboard + chat + patching)
aws cognito-idp admin-add-user-to-group --user-pool-id $POOL_ID \
  --username user@example.com --group-name operators

# Read-only access (dashboard only, no chat or patching)
aws cognito-idp admin-add-user-to-group --user-pool-id $POOL_ID \
  --username user@example.com --group-name viewers
```

Two groups are created automatically by the CDK stack:
- `operators` — full access: dashboard, chat, patch execution, compliance reports
- `viewers` — read-only: dashboard and compliance reports only

> **Note**: If the Cognito User Pool is recreated (e.g., after a CDK change that replaces the pool resource), all users are lost. The Pool ID is in the CloudFormation outputs — always verify it before creating users. To federate with your corporate IdP instead of managing users manually, see [Production Hardening → Federating with Okta, Ping, or Azure AD](#federating-with-okta-ping-or-azure-ad).

### Long-Term Memory (Automatic)

Long-term memory is configured automatically during `./deploy.sh`. The script:
1. Upgrades memory mode from `STM_ONLY` to `STM_AND_LTM`
2. Adds SEMANTIC and SUMMARY extraction strategies
3. Validates the runtime role's IAM policy references the correct memory resource

If you need to re-run memory setup manually (e.g., after recreating the memory resource):
```bash
source venv/bin/activate
python agent/setup_memory_strategies.py
```

This adds SEMANTIC (fact extraction) and SUMMARY (session summaries) strategies to the existing memory resource. Memories are automatically scoped per operator via `actor_id` — Operator A's extracted facts are invisible to Operator B.

**How memory works:**

- **Short-term (STM)**: Conversation turns are persisted to AgentCore Memory via `AgentCoreMemorySessionManager` (the official Strands integration). Sessions survive page refreshes — the frontend stores the `session_id` in localStorage.
- **Long-term (LTM)**: After each conversation, AgentCore asynchronously extracts semantic facts and session summaries. On the next session, the agent retrieves relevant LTM records and injects them as `<user_context>` before the first model call.
- **Operator isolation**: The `actor_id` is scoped as `patch-automation/{operator_identity}`, ensuring memory isolation between operators. Invalid characters (like `@`) are automatically sanitized.

**Important**: The AgentCore toolkit automatically injects the memory_id as `BEDROCK_AGENTCORE_MEMORY_ID` into the generated Dockerfile. No custom Dockerfile is needed — the toolkit handles memory_id, OTEL instrumentation, and `PYTHONUNBUFFERED` automatically.

### Bedrock Guardrails (Optional)

Creates a sample guardrail with topic filtering, content safety, and sensitive data masking.

```bash
source venv/bin/activate
python agent/setup_guardrail.py
```

Then add the output values to your deployment environment:
```bash
# In .env or container environment
GUARDRAIL_ID=<guardrail-id-from-output>
GUARDRAIL_VERSION=<version-from-output>
```

Redeploy the agent to pick up the guardrail: `./deploy.sh agent`

The sample guardrail includes:
- Denied topic: non-patching queries (general knowledge, unrelated AWS services)
- Content filters: blocks harmful content at HIGH threshold
- Sensitive data: anonymizes AWS account IDs and IP addresses in responses
- Prompt attack detection: blocks prompt injection attempts

Extend it in the Bedrock console or via API with additional policies for your compliance requirements (PII redaction, custom word filters, additional denied topics).

---

## Configuration

### SLA Policies

SLA thresholds are read directly from EC2 instance tags. The solution has no built-in knowledge of compliance frameworks — customers define their own SLA hours per instance via tags.

**Recommended tags** (hours) — falls back to defaults if missing:
- `SLA-CRITICAL` — e.g., `12` (patch within 12 hours)
- `SLA-HIGH` — e.g., `48`
- `SLA-MEDIUM` — e.g., `168`
- `SLA-LOW` — e.g., `720`

If an instance is missing SLA tags, the solution falls back to global defaults: 24h critical, 72h high, 168h medium, 720h low.

The `ComplianceFrameworks` tag (e.g., `PCI-DSS,SOC2`) is optional — it's included in compliance reports for audit context (tells auditors *why* the SLA was set) but does not drive SLA calculation.

**Sample environment SLA tags:**

| Environment | Framework | SLA-CRITICAL | SLA-HIGH | SLA-MEDIUM | SLA-LOW |
|-------------|-----------|-------------|----------|------------|---------|
| dev (all) | SOC2 | 24h | 72h | 168h | 720h |
| staging (PCI-DSS) | PCI-DSS | 12h | 48h | 168h | 720h |
| staging (others) | SOC2 | 24h | 72h | 168h | 720h |
| prod (PCI-DSS) | PCI-DSS | 6h | 24h | 168h | 720h |
| prod (others) | SOC2/HIPAA | 24h | 72h | 168h | 720h |

> **Enterprise integration**: Most organisations manage SLA thresholds in their CMDB or GRC platform. Push SLA hours to EC2 tags via AWS Organizations tag policies, Service Catalog, or your CI/CD pipeline. The agent reads them at runtime — no redeployment needed when thresholds change.

### Maintenance Windows

The agent respects SSM maintenance windows:
- SCHEDULED: next window is within SLA → wait for it
- EMERGENCY: next window exceeds SLA → patch immediately

Sample windows (from `--with-sample-env`): dev daily 1AM UTC, staging Tuesday 2AM UTC, prod 1st of month 2AM UTC.

> **Enterprise integration**: If your organisation manages change windows through a change management system (ServiceNow Change Management, Jira change requests, PagerDuty), you can extend the agent to query your change calendar API instead of (or in addition to) SSM maintenance windows. The agent's SLA-vs-window decision logic is in `execute_patch_operation()` — it just needs a "next available window" timestamp, regardless of where that comes from. For organisations using both SSM windows and a change management system, the agent can check both and use the earliest compliant window.

---

## Web UI

Browser-based operations console with dashboard and conversational chat.

- Dashboard: KPI bar, environment status cards, vulnerability table, compliance summary, decision explainability table
- Chat: streaming responses, specialist indicators, approval buttons for phased rollout

### Accessing the UI

By default, the UI deploys with Cognito authentication on a public (internet-facing) ALB with HTTPS. After deployment, the URL is printed in the terminal output and available in CloudFormation outputs:

```bash
# Get the UI URL
aws cloudformation describe-stacks --stack-name Patchy-UI \
  --query 'Stacks[0].Outputs[?OutputKey==`UIUrl`].OutputValue' --output text
```

Open the URL in your browser. You'll be redirected to the Cognito hosted login page. Log in with the credentials you created in [User Management](#user-management).

> **Self-signed certificate warning**: If you used `setup-tls.sh` (the default), your browser will show a certificate warning. This is expected — click through to proceed. For production, replace with an ACM public certificate (free, auto-renewing, no warnings).

#### Internal ALB mode (no Cognito)

To deploy without Cognito on an internal ALB (e.g., for VPN-only access), set `COGNITO_ENABLED=false` in `.env`. Access via SSM port forwarding:

```bash
./connect-ui.sh       # Start tunnel, then open https://localhost:8443
```

Requires the AWS CLI Session Manager plugin and at least one SSM-managed instance in the VPC.

### TLS / HTTPS

| Approach | When to use |
|----------|-------------|
| `./setup-tls.sh` | Quick start / pilot. Auto-run by `deploy.sh`. Browser shows cert warning (self-signed). |
| ACM public certificate | Production. Set ARN in `.env`. Free, auto-renewing, no warnings. |
| No certificate + `COGNITO_ENABLED=false` | Internal ALB only. ALB serves HTTP on port 80. |

> **Note**: Cognito requires HTTPS. If `ACM_CERTIFICATE_ARN` is not set, `deploy.sh` automatically runs `setup-tls.sh` to generate a self-signed certificate.

### Local Development

```bash
cd ui/api && python3 server.py        # Backend on port 8000
cd ui/frontend && npm run dev          # Frontend on port 5173
```

---

## Observability

The agent ships with observability via OpenTelemetry auto-instrumentation and CloudWatch.

### Important: Logging Configuration

AgentCore containers run with OTEL auto-instrumentation (`opentelemetry-instrument` via `aws-opentelemetry-distro`). The OTEL log pipeline captures Python `logging` module output — **not** raw `print()` to stderr. All agent code uses `logging.getLogger()` to ensure logs are captured.

If you extend the agent with custom code, always use Python's `logging` module:
```python
import logging
logger = logging.getLogger(__name__)
logger.info("This will appear in CloudWatch via OTEL")
# print("This will NOT appear in CloudWatch", file=sys.stderr)  # Don't do this
```

### Log Streams

All logs flow to CloudWatch under `/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT/`:

| Stream pattern | Content |
|----------------|---------|
| `YYYY/MM/DD/[runtime-logs]<uuid>` | Application logs (`[SUPERVISOR]`, `[MEMORY]`), Strands debug, memory lifecycle |
| `otel-rt-logs` | OTEL structured JSON logs with `traceId`/`spanId` for end-to-end correlation |

The UI container logs to `/patch-automation/ui` (FastAPI access logs, chat request metadata).

### Finding Your Runtime ID

The runtime ID is needed for log queries. Find it with:
```bash
# From agentcore CLI
source venv/bin/activate && cd agent && agentcore status

# Or from the API
aws bedrock-agentcore-control list-agent-runtimes \
  --query 'agentRuntimes[0].agentRuntimeId' --output text
```

### Tailing Logs

```bash
# Real-time agent logs (all streams)
aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT \
  --log-stream-name-prefix "$(date -u +%Y/%m/%d)/[runtime-logs]" --follow

# Filter to application-level events
aws logs filter-log-events \
  --log-group-name "/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT" \
  --filter-pattern "SUPERVISOR" --start-time $(date -v-1H +%s)000

# Memory operations
aws logs filter-log-events \
  --log-group-name "/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT" \
  --filter-pattern "customer context" --start-time $(date -v-1H +%s)000

# Errors only
aws logs filter-log-events \
  --log-group-name "/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT" \
  --filter-pattern "ERROR" --start-time $(date -v-1H +%s)000
```

### OTEL Traces and GenAI Dashboard

The agent container runs with `opentelemetry-instrument` (via `aws-opentelemetry-distro`), which auto-instruments all Bedrock model calls, tool executions, and memory operations. Every log entry includes `traceId` and `spanId` for correlation.

View traces: `CloudWatch Console → Transaction Search → filter by service name`

View agent metrics: `CloudWatch Console → GenAI Observability → AgentCore` for token usage, latency, session counts, and per-tool execution times.

### What's Logged

- Supervisor routing decisions (which specialist, why)
- Memory lifecycle: session creation, STM event writes, LTM retrieval counts
- Bedrock model calls: request/response payloads, token counts, latency
- Tool execution: tool name, duration, success/failure
- Specialist handoffs: query enrichment, context forwarding

Every agent decision is logged to CloudWatch and S3 compliance reports. CloudWatch Insights query:
```
fields @timestamp, agent, decision_type, decision, reasoning
| filter @message like /DECISION:/
| sort @timestamp desc
```

### Audit Trail Queries

The per-user audit trail is queryable from multiple surfaces:

**SSM Command History** — find all patches initiated by a specific operator:
```bash
aws ssm list-commands \
  --filters "Key=Comment,Values=operator@" \
  --query "Commands[].{Id:CommandId,Comment:Comment,Status:Status,Requested:RequestedDateTime}" \
  --output table
```

**S3 Compliance Reports** — query with Athena for fleet-wide audit:
```sql
SELECT report_id, timestamp, operator, cve_id, environment, decision, sla_met
FROM patch_compliance_reports
WHERE operator LIKE '%jane.doe%'
ORDER BY timestamp DESC
```

**CloudWatch Logs Insights** — real-time operator activity:
```
fields @timestamp, @message
| filter @message like /PATCH_EXECUTED:/ or @message like /PATCH_SCHEDULED:/
| parse @message "operator=* " as operator
| sort @timestamp desc
```

---

## Security

- Cognito authentication on public ALB (default) — users must log in via Cognito hosted UI
- Internal ALB mode available (`COGNITO_ENABLED=false`) — accessed via SSM port forwarding, no public exposure
- HTTPS via ACM certificate (required for Cognito, optional for internal mode)
- Role-based access control via Cognito groups: `operators` (full access) and `viewers` (dashboard read-only, no chat/patching)
- Rate limiting (20 req/min per client IP)
- Instance ID regex validation on all SSM operations
- IAM least privilege — write access scoped to SSM commands and S3 compliance bucket
- Human approval gates between environments during phased rollout
- Dry-run gate — programmatic enforcement requires a recent Scan before any Install operation
- Per-user audit trail — operator identity (from Cognito JWT) recorded in S3 reports, SSM comments, and CloudWatch logs

---

## Production Hardening

The solution ships ready for pilot use. For production workloads, consider the following:

### Authentication and Authorization

Cognito authentication is built in and enabled by default. The ALB authenticates users via the Cognito hosted UI before forwarding requests to Fargate. Two groups (`operators`, `viewers`) control access.

For production, consider these enhancements:

1. **Federate with your corporate IdP**: See [Federating with Okta, Ping, or Azure AD](#federating-with-okta-ping-or-azure-ad) below. This eliminates manual user management in Cognito.
2. **IAM session tags for CloudTrail attribution**: When invoking the agent, assume the execution role with `sts:AssumeRole` and pass `--tags operator={email}`. CloudTrail then records the human identity on every downstream API call (SSM SendCommand, S3 PutObject) — giving you tamper-proof, native audit attribution without any custom logging code.
3. **Custom domain**: Replace the ALB DNS name with a Route 53 alias to your own domain (e.g., `patchy.yourcompany.com`) and use an ACM public certificate for that domain. This eliminates the self-signed certificate warning and gives users a memorable URL.

### Federating with Okta, Ping, or Azure AD

If your organisation already has an IdP, you don't need to manage users in Cognito directly. Cognito acts as a thin OIDC/SAML proxy — users log in via your corporate IdP and Cognito issues the JWT that the ALB validates. No app code changes, no ALB changes.

**Steps (Okta example, ~15 min):**
1. In Okta: create a new OIDC app, set the callback URL to `https://<alb-dns>/oauth2/idpresponse`
2. In Cognito: add Okta as an OIDC identity provider (Console → User Pool → Sign-in experience → Federated identity provider sign-in)
3. Map Okta groups to Cognito groups (`operators`, `viewers`) via attribute mapping
4. Update the User Pool Client's `supportedIdentityProviders` to include the Okta provider

Users are redirected straight to Okta's login page — Cognito's hosted UI is bypassed entirely. Group membership flows through as JWT claims and the backend resolves roles identically.

### Native AWS Audit Capabilities

Before building custom audit infrastructure, consider what AWS already provides:

- **CloudTrail**: Every SSM `SendCommand`, S3 `PutObject`, and EC2 API call is already logged with the IAM principal, source IP, timestamp, and full request parameters. With session tags (see above), CloudTrail attributes actions to the human operator, not just the service role.
- **SSM Command History**: `ssm:ListCommands` and `ssm:ListCommandInvocations` return the full execution history including the `Comment` field (which this solution populates with operator identity), status, timing, and target instances.
- **S3 Object Metadata**: Compliance reports are stored with S3 object metadata (`operator`, `cve-id`, `environment`, `severity`, `decision-type`, `sla-met`, `team`, `product`). Queryable via S3 Inventory + Athena without reading the JSON body.
- **CloudWatch Logs Insights**: Agent logs include structured `PATCH_SCHEDULED:` and `PATCH_EXECUTED:` entries with operator identity, environment, mode, and instance count — queryable in real time.

For production, the recommended approach is: Cognito for authentication → IAM session tags for attribution → CloudTrail as the single source of truth for audit. The custom S3 compliance reports complement this with business-level context (SLA assessment, compliance delta) that CloudTrail doesn't capture.

### Integration Patterns

The solution exposes an HTTP API with SSE streaming, making it embeddable in existing tools:

- **Slack / Microsoft Teams**: POST to `/api/chat` with the message body, consume the SSE stream, and render agent responses in a channel. A lightweight Lambda or Fargate sidecar can bridge the Slack Events API to the patch automation API. The agent's structured next-steps output maps naturally to Slack interactive buttons.
- **ServiceNow / Jira**: Trigger patch operations from incident tickets. On ticket creation with a CVE tag, call the API to assess impact. On approval, call the API to execute. Write the compliance report URL back to the ticket.
- **SNS Notifications**: After long-running patch operations, publish a completion event to SNS. Subscribe Slack webhooks, email, or PagerDuty for operator notification without polling the chat.
- **Custom dashboards**: The `/api/dashboard` endpoint returns all fleet status, vulnerability, and compliance data as JSON — embeddable in Grafana, Datadog, or internal portals.

### Network and Access

- **Custom domain**: Register a domain in Route 53, create an alias record pointing to the ALB, and use an ACM public certificate for that domain. This gives users a clean URL (e.g., `https://patchy.yourcompany.com`) and eliminates the self-signed certificate warning.
- **AWS Client VPN**: For internal-ALB mode (`COGNITO_ENABLED=false`), deploy a Client VPN endpoint in the VPC for broader team access beyond individual SSM tunnels.
- **VPC endpoints**: Add interface VPC endpoints for SSM, EC2, S3, STS, and Bedrock to eliminate NAT gateway traffic and reduce data transfer costs. Also improves latency for API calls.
- **WAF**: Add AWS WAF to the public ALB for request filtering, SQL injection protection, rate limiting, and managed rule sets as defense-in-depth.

### Data and Compliance

- **S3 bucket retention**: Change `removalPolicy` to `RETAIN` and remove `autoDeleteObjects` in `core-stack.ts` to preserve compliance reports on stack teardown. Current default is `DESTROY` for clean teardown.
- **S3 Object Lock**: Enable Object Lock (compliance mode) on the compliance reports bucket to make audit data immutable. Prevents accidental or malicious deletion of compliance evidence.
- **KMS encryption**: Replace S3-managed encryption with a customer-managed KMS key for compliance reports. Enables key rotation policies and CloudTrail key usage auditing.
- **Cross-account backup**: Replicate the compliance reports bucket to a separate audit account using S3 Cross-Region Replication. Ensures reports survive even if the primary account is compromised.

### Availability and Scaling

- **Multi-AZ Fargate**: Increase `desiredCount` to 2+ in `ui-stack.ts` for high availability. The ALB already spans multiple AZs.
- **Auto-scaling**: Add ECS Service Auto Scaling based on CPU/memory utilization or request count to handle variable operator load.
- **Multi-region**: Deploy the infrastructure stack to a secondary region with Route 53 health checks for disaster recovery. The agent is stateless, so failover is straightforward.
- **ALB health check tuning**: Tighten health check thresholds for faster failover. Consider adding a deeper health check endpoint that validates downstream connectivity (SSM, Bedrock).

### Agent Reliability

- **Guardrails**: Amazon Bedrock Guardrails are supported out of the box. Run `python agent/setup_guardrail.py` to create a sample guardrail with a denied topic (non-patching queries), content filters, and sensitive data masking (AWS account IDs, IP addresses). Then set `GUARDRAIL_ID` and `GUARDRAIL_VERSION` environment variables — the agent picks them up automatically. The sample guardrail is a starting point; extend it with additional denied topics, PII redaction policies, and custom word filters for your compliance requirements. See [Amazon Bedrock Guardrails documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html) for the full policy options.
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

- Replace the self-signed certificate with an ACM public certificate tied to your domain (free, auto-renewing, no browser warnings). Request the certificate in ACM with DNS validation, add the CNAME record to Route 53, and set the ARN in `.env` as `ACM_CERTIFICATE_ARN`. Redeploy with `./deploy.sh ui`.

---

## Teardown

```bash
./deploy.sh destroy --sample-only    # Remove sample environment only
./deploy.sh destroy                # Remove ALL infrastructure (prompts for confirmation)
agentcore destroy                  # Remove the AgentCore agent runtime separately
```

S3 buckets are auto-emptied and deleted with the stack. No manual cleanup needed.

> **Note:** `agentcore destroy` removes the agent runtime but leaves the memory resource intact (it may contain audit-relevant conversation history). To delete memory: `aws bedrock-agentcore-control delete-memory --memory-id <memory-id>`.

> **Note:** The Cognito User Pool is deleted when `COGNITO_ENABLED=false` is set and the UI is redeployed. A dormant pool with credentials is a security liability. Re-enabling Cognito creates a fresh pool — users will need to be re-invited. If you need to preserve users across a temporary mode switch, export them first with `aws cognito-idp list-users`.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Agent not responding | `agentcore status` then `agentcore launch --auto-update-on-conflict` |
| Permission errors | Check `Patchy-Core` stack outputs |
| No instances found | Instances need SSM Agent + `Environment` tag. Check: `aws ssm describe-instance-information` |
| UI container unhealthy | Check logs: `aws logs tail /patch-automation/ui --follow` |
| SSM tunnel fails | Ensure Session Manager plugin is installed and at least one instance is online |
| Cognito login redirect error | Verify the callback URL matches the ALB DNS. ALB DNS is mixed-case but Cognito compares case-sensitively. The CDK stack handles this via a Lambda-backed custom resource that lowercases the DNS. If you see `redirect_mismatch`, redeploy: `./deploy.sh ui` |
| Cognito users missing after redeploy | If the User Pool was recreated (new Pool ID in CloudFormation outputs), all users are lost. Recreate them — see [User Management](#user-management) |
| Can't access UI URL | Check ALB is `internet-facing` (not `internal`). Verify security group allows inbound 443. If using `COGNITO_ENABLED=false`, use `./connect-ui.sh` instead |
| Self-signed cert warning | Expected with `setup-tls.sh`. Click through in browser. For production, use an ACM public certificate |
| Agent logs missing from CloudWatch | Ensure all agent code uses Python `logging` module, not `print()` to stderr. See [Observability → Logging Configuration](#important-logging-configuration) |
| Stale config after account migration | Delete `agent/.bedrock_agentcore.yaml` and redeploy: `./deploy.sh agent`. The deploy script auto-detects stale configs from different accounts. |
| Memory not persisting | Check `agentcore status` for memory errors. Verify the memory resource exists: `aws bedrock-agentcore-control list-memories` |
| Agent asks for info already provided | This is a context-forwarding issue. The supervisor should enrich queries with CVE IDs and instance IDs from previous turns. If it persists, check that memory is active and the session_id is consistent across requests. |

### Deploying to a New Account

When moving the solution to a different AWS account:

1. Update `.env` with the new `AWS_PROFILE` (and optionally `AWS_REGION`)
2. Delete `agent/.bedrock_agentcore.yaml` (it contains the old account's agent ARN, memory ID, and ECR repo)
3. Run `./deploy.sh --with-sample-env` — the script handles everything: fresh `agentcore configure`, memory mode upgrade to `STM_AND_LTM`, LTM strategy setup, and IAM policy validation

The deploy script auto-detects stale configs (wrong account), upgrades memory mode, configures LTM strategies, and fixes IAM permissions — no manual steps needed.

### Known Limitations

- Manual approval required between environments in phased rollout
- CloudWatch alarms must be pre-configured for application-layer health checks
- Linux-only patching and rollback (Amazon Linux 2 / yum-based). See note at top for adapting to other OS types.
- Single account and region deployment (multi-account/region extension planned — see `.kiro/specs/multi-account-region/`)
- Self-signed TLS certificate shows browser warning (replace with ACM public cert for production)
- Severity-scoped patching applies all patches at the requested severity level, not individual CVEs. SSM Patch Manager does not support per-CVE installation. A future enhancement using `InstallOverrideList` with CVE → package mapping from Inspector could enable surgical per-CVE patching.
- Baseline override files are pre-generated for Amazon Linux 2. Other OS types require creating equivalent override JSON files.

---

## Extending the Solution

The multi-agent architecture is designed for extensibility. Each specialist agent has its own tool set — adding a new capability is a single tool function with no changes to routing or decision logic.

### What the Solution Covers Today

The solution handles the core enterprise patching workflow end-to-end:

- **Vulnerability discovery** — scan your fleet for CVEs, assess severity, and identify which environments and instances are affected
- **Fleet-wide impact assessment** — understand how widespread a vulnerability is across dev, staging, and prod before deciding how to respond
- **Patch impact preview** — see exactly which packages will be updated before committing, including packages beyond the target CVE (the "blast radius" of the patch operation)
- **SLA-driven decision making** — automatically determine whether to patch immediately (emergency) or wait for the next maintenance window (scheduled), based on per-instance SLA tags
- **Patch policy awareness** — detect existing SSM Patch Policies and respect them for scheduled operations instead of creating duplicate patching tasks
- **Severity-scoped execution** — apply patches at a specific severity level rather than applying everything, with operator confirmation before execution
- **Application health verification** — check SSM connectivity and CloudWatch alarms after patching to confirm instances are healthy
- **Automated rollback** — reverse patches if health checks fail, with verification that the rollback succeeded
- **Compliance reporting** — generate structured audit reports with before/after compliance delta, SLA assessment, and operator identity
- **Cross-session memory** — the agent remembers previous patching outcomes, CVE history, and operator preferences across conversations
- **Real-time operations console** — dashboard with fleet status, vulnerability counts, compliance metrics, and decision audit trail

### What Customers Can Extend

| Capability | What it adds | How to integrate |
|-----------|-------------|-----------------|
| **CMDB-driven blast radius** | Show application dependencies affected by patching ("patching this instance impacts 3 upstream services") | Add a tool that queries your CMDB (ServiceNow, Device42) for instance dependencies. The agent presents the dependency map before the operator confirms. |
| **Change management integration** | Check your change calendar before patching, not just SSM maintenance windows | Add a tool that queries your change management system (ServiceNow, Jira). The agent checks both SSM windows and your change calendar for the next available slot. |
| **Third-party vulnerability scanners** | Use Qualys, Tenable, or Rapid7 instead of (or alongside) Amazon Inspector | Add a tool that queries your scanner's API. The vulnerability analyst uses it alongside or instead of Inspector findings. |
| **Multi-OS patching** | Support Ubuntu, RHEL, Windows alongside Amazon Linux 2 | Create baseline override JSON files per OS. Adapt rollback commands for apt/zypper/Windows Update. SSM Patch Manager and Inspector already support all OS types. |
| **Slack/Teams notifications** | Get notified when patching completes or fails | POST to `/api/chat` from a Lambda triggered by SNS. The agent's structured output maps to Slack message blocks. |
| **Per-CVE surgical patching** | Install only the specific package that fixes a CVE, not all packages at that severity | Use SSM's `InstallOverrideList` parameter with CVE → package name mapping from Inspector's `vulnerablePackages` field. |
| **Cross-account patching** | Manage instances across multiple AWS accounts from a single agent | Add assume-role logic to operate in spoke accounts. The dashboard API's `scopes` array already supports multiple accounts. |

Each extension is a single tool function added to the relevant specialist agent — no changes to the supervisor routing, memory, or UI.

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
Who patched prod last week?
Generate a compliance report for PCI-DSS in staging
```

The agent will automatically assess fleet-wide impact when scanning vulnerabilities, run a dry-run preview before patching, capture before/after compliance snapshots, and include the compliance delta in the report. If rollback is needed, it verifies the rollback succeeded before reporting back.
