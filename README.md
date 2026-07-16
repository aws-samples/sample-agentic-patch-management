# Intelligent Patch Automation for AWS — AI-Driven Vulnerability Response

Unified AI agent for automated, vulnerability-driven patch management using [Strands Agents SDK](https://github.com/strands-agents/sdk-python), [Amazon Bedrock AgentCore](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-core.html), and [AWS Systems Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/what-is-systems-manager.html).

```
You: Handle CVE-2025-38477 in dev environment
Agent: Checking maintenance window... Emergency patching required.
       Patching dev with approval gates for staging/prod.
       Complete. Compliance report saved to S3.
```

Natural language in -> risk-based decision -> automated execution. No browser tabs, no manual triage.

---

## Prerequisites

### Local Tooling

- **Python 3.11+** — [python.org](https://www.python.org/downloads/)
- **Node.js 18+** — [nodejs.org](https://nodejs.org/)
- **Docker** or **[Finch](https://github.com/runfinch/finch)** — CDK builds the UI container image locally. `deploy.sh` auto-detects Finch.
- **AWS CLI v2** — [Install guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)

### AWS Account Setup

Complete these **before** running `deploy.sh`.

| Step | What to do |
|------|-----------|
| **1. Amazon Bedrock** | [Enable model access](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access-modify.html) for Claude Sonnet in your target region |
| **2. Quick Setup** | Run 3 configs from the management account or [DA account](docs/extending.md#delegated-administrator-deployment) (Console → Systems Manager → Quick Setup): **a)** [Default Host Management Configuration](https://docs.aws.amazon.com/systems-manager/latest/userguide/quick-setup-default-host-management-configuration.html), **b)** [Host Management](https://docs.aws.amazon.com/systems-manager/latest/userguide/quick-setup-host-management.html) (entire org, all regions — enable all options), **c)** [Config Recording](https://docs.aws.amazon.com/systems-manager/latest/userguide/quick-setup-config.html) (entire org, all regions — all supported resource types + include global resources). First-time Config delay: up to 6 hours. |
| **3. SSM Explorer** | Console → Systems Manager → Explorer → **Enable Explorer** ([Integrated Setup](https://docs.aws.amazon.com/systems-manager/latest/userguide/Explorer-setup.html)) |
| **4. Amazon Inspector** | [Enable Inspector](https://docs.aws.amazon.com/inspector/latest/user/getting_started_tutorial.html) for EC2 scanning |
| **5. EC2 Instance Tags** | Tag each managed instance: `Environment`=dev/staging/prod, `PatchAutomation`=enabled. SSM Agent + [`AmazonSSMManagedInstanceCore`](https://docs.aws.amazon.com/systems-manager/latest/userguide/setup-instance-permissions.html) required. |
| **6. Multi-Account** *(optional)* | See [Multi-Account Setup](#multi-account-setup) |

> **Control Tower**: Accounts directly under the org root (not in an OU) are not targeted by Quick Setup. Move them into an OU first.

`deploy.sh` creates the remaining infrastructure automatically (AgentCore, IAM, S3, Resource Data Sync, ECS, ALB, Cognito).

For how fleet discovery works, see [Architecture — Fleet Discovery](docs/architecture.md#fleet-discovery). For post-deploy verification and troubleshooting, see [Operations — Verifying Fleet Discovery](docs/operations.md#verifying-fleet-discovery).

---

## Quick Start

Ensure you've completed the [Prerequisites](#prerequisites) above. The deploy script handles the full sequence: Python venv, agent deployment, role detection, CDK bootstrap, 4 infrastructure stacks, TLS, Cognito, Docker build, and Fargate deployment.

**1. Configure**

```bash
cp .env.example .env
# Edit .env — set AWS_PROFILE and AWS_REGION
```

**2. Deploy**

```bash
./deploy.sh
# ~25-30 minutes on first run

# Optional: deploy 15 sample EC2 instances for testing
./sample-env.sh deploy
```

**3. Create a Cognito user** (for web UI login)

```bash
POOL_ID=$(aws cloudformation describe-stacks --stack-name Patchy-UI \
  --query 'Stacks[0].Outputs[?OutputKey==`CognitoUserPoolId`].OutputValue' --output text)

# Create user, set permanent password, grant operator role
aws cognito-idp admin-create-user --user-pool-id $POOL_ID \
  --username your-email@example.com \
  --user-attributes Name=email,Value=your-email@example.com Name=email_verified,Value=true \
  --temporary-password 'TempPass1!' --message-action SUPPRESS

aws cognito-idp admin-set-user-password --user-pool-id $POOL_ID \
  --username your-email@example.com --password 'YourSecurePass1!' --permanent

aws cognito-idp admin-add-user-to-group --user-pool-id $POOL_ID \
  --username your-email@example.com --group-name operators
```

> See [Operations — User Management](docs/operations.md) for `viewers` role setup and user lifecycle.

**4. Verify and open**

```bash
UI_URL=$(aws cloudformation describe-stacks --stack-name Patchy-UI \
  --query 'Stacks[0].Outputs[?OutputKey==`UIUrl`].OutputValue' --output text)
echo "UI URL: $UI_URL"
curl -sk "$UI_URL/api/health"   # Should return: {"status":"ok"}
```

Open the URL in your browser and log in. You'll see a self-signed cert warning — click through (expected with the default setup; see [Security](docs/security.md) for ACM certificate setup).

---

## What It Looks Like

```
You: What critical vulnerabilities do we have in dev?

Agent: CRITICAL  2 vulnerabilities across 5 instances in dev

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

From there, the agent runs a dry-run scan (showing exactly which patches will be installed per instance), waits for operator approval, executes, verifies health, and generates a compliance report — all in the same conversation.

**More example prompts:**

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

Read these before deciding to adopt — they define the current scope:

| Limitation | Detail | Path forward |
|------------|--------|--------------|
| **Amazon Linux 2 only** | Rollback uses `yum history undo`. AWS Systems Manager Patch Manager and Amazon Inspector are OS-agnostic — only the rollback command needs adaptation. | See [Extending the Solution](docs/extending.md) |
| **Multi-account requires setup** | Cross-account patching uses [SSM Automation with TargetLocations](https://docs.aws.amazon.com/systems-manager/latest/userguide/running-automations-multiple-accounts-regions.html). | See [Multi-Account Setup](#multi-account-setup) below |
| **Severity-level patching, not per-CVE** | [SSM Patch Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/patch-manager.html) cannot install a single CVE's patch in isolation. The agent scopes to severity level (e.g., all CRITICAL) using [BaselineOverride](https://docs.aws.amazon.com/systems-manager/latest/userguide/patch-manager-about-aws-runpatchbaseline.html#patch-manager-about-aws-runpatchbaseline-parameters-baselineoverride) files. | See [Severity-Scoped Patching](docs/architecture.md#severity-scoped-patching) |
| **Manual approval between environments** | The agent asks the operator before proceeding to the next environment in staged rollout. | By design — safety gate |
| **Self-signed TLS by default** | Browser shows a warning on first visit. | Replace with [ACM public certificate](https://docs.aws.amazon.com/acm/latest/userguide/gs.html) for production. See [Security](docs/security.md) |
| **CloudWatch Alarms not auto-created** | The agent reads existing [CloudWatch Alarms](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/AlarmThatSendsEmail.html) for post-patch health checks but does not create them. | Pre-configure alarms for your application |

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
                        +---------------------v------------------------+
                        |           Patch Automation Agent             |
                        |   (Claude Sonnet — 24 tools, direct         |
                        |    tool selection)                           |
                        +---+--------+--------+--------+--------------+
                            |        |        |        |
              +-------------v--+ +---v--------v--+ +---v--------------+
              | Amazon         | | AWS Systems   | | Amazon S3        |
              | Inspector      | | Manager       | | (Compliance      |
              | (Vulnerability | | (Patch, cmds, | |  reports,        |
              |  findings)     | |  windows)     | |  baselines,      |
              +----------------+ +--------------+  |  audit trail)    |
                                                   +------------------+
```

> **Architecture:** A single agent with 24 tools and direct tool selection.

| Aspect | Detail |
|--------|--------|
| Model | Claude Sonnet (`us.anthropic.claude-sonnet-5`) |
| Tools | 24 tools across vulnerability, patch, fleet, maintenance, and compliance domains |
| Memory | Read/Write (STM via `AgentCoreMemorySessionManager`) |
| Steering | `PatchWorkflowSteering` + `ComplianceOutputSteering` + `ConfirmationGoalHandler` |
| Tool selection | Direct — the model selects tools based on `Decision:` docstrings and `next_action` hints |

**Patch execution workflow** (enforced sequence — the agent will not skip steps):

| Step | What happens |
|------|-------------|
| 1. Discover | Get [patch compliance state](https://docs.aws.amazon.com/systems-manager/latest/userguide/sysman-compliance-about.html) and SLA requirement for target instances |
| 2. Check windows + policy | Find next [SSM Maintenance Window](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-maintenance.html); check for existing [Patch Policy](https://docs.aws.amazon.com/systems-manager/latest/userguide/patch-manager-policies.html) associations. If an Install policy already covers these instances and the SLA window allows it, the agent defers to the existing policy. |
| 3. SLA decision | Window within SLA -> SCHEDULED. Window exceeds SLA -> EMERGENCY (patch now) |
| 4. Dry-run scan | [`AWS-RunPatchBaseline`](https://docs.aws.amazon.com/systems-manager/latest/userguide/patch-manager-about-aws-runpatchbaseline.html) with `Operation: Scan`. Uses a [`BaselineOverride`](https://docs.aws.amazon.com/systems-manager/latest/userguide/patch-manager-about-aws-runpatchbaseline.html#patch-manager-about-aws-runpatchbaseline-parameters-baselineoverride) when severity-scoped. Operator reviews before proceeding. **Code-enforced: no Install without a Scan within the last 2 hours.** |
| 5. Pre-patch snapshot | Capture compliance metrics per instance for before/after comparison |
| 6. Run | `AWS-RunPatchBaseline` with `Operation: Install` |
| 7. Health check | Verify SSM connectivity and [CloudWatch Alarms](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/AlarmThatSendsEmail.html) |
| 8. Post-patch snapshot | Capture compliance metrics after patching |
| 9. CVE verification | Query [Inspector](https://docs.aws.amazon.com/inspector/latest/user/what-is-inspector.html) to confirm CVEs are remediated |
| 10. Compliance report | JSON report with before/after delta, SLA result, operator identity -> S3 |

For detailed architecture documentation (memory model, SLA decision flow, severity-scoped patching, rollback verification, patch policy integration), see [Architecture](docs/architecture.md).

**Decision architecture** — the agent chooses the correct tool with minimal prompting via 5 structural layers:

1. Tool results include `next_action` hints guiding the model's next step
2. Tool docstrings include `Decision:` lines encoding routing intent
3. System prompt contains only counter-intuitive rules (~300 tokens)
4. `ConfirmationGoalHandler` enforces the confirm/retry state machine
5. Pre-deploy eval validates tool selection hasn't regressed

See [Agent Decision Architecture](docs/architecture.md#agent-decision-architecture) for details.

**Key files:**

| File | Purpose |
|------|---------|
| `agent/supervisor.py` | Agent entrypoint — system prompt, 24 tools, streaming |
| `agent/helper/tools/` | Domain-split tool modules (24 tools): vulnerability, patch, fleet, maintenance, compliance |
| `agent/helper/steering.py` | Deterministic workflow enforcement (path routing, CVE forwarding, cross-env gates) |
| `agent/helper/goals.py` | Confirmation retry state machine |
| `agent/helper/agent_factory.py` | Agent creation with session manager + plugins |
| `agent/eval/` | Pre-deploy tool selection eval (scenarios, baseline, runner) |
| `ui/api/server.py` | FastAPI proxy, dashboard API, auth middleware |
| `ui/frontend/src/App.tsx` | React UI -- dashboard + chat + command palette |
| `infra/lib/` | CDK stacks (Network, Core, UI, SampleEnv) |

---

## Deployment

### Estimated Cost (Pilot)

| Component | Approximate Cost | Notes |
|-----------|-----------------|-------|
| NAT Gateways | ~$32-64/month | 1 per account (hub + spoke if using sample env) |
| ECS Fargate (1 task, 256 CPU / 512 MB) | ~$10/month | UI container |
| Application Load Balancer | ~$16/month + data transfer | Public or internal |
| Bedrock (Claude Sonnet) | Variable | ~$3/1M input tokens, ~$15/1M output tokens |
| Inspector | Per-instance | ~$1.25/instance/month for EC2 scanning |
| Sample EC2 instances (5x t3.micro per account) | ~$25-50/month | Hub only: 5 instances. Multi-account: 5 hub + 5 spoke = 10 |
| **Total (pilot, hub only)** | **~$100-130/month** | Without sample env in spoke |
| **Total (pilot, multi-account with sample env)** | **~$160-190/month** | Includes spoke VPC + NAT + 5 instances |

Costs scale with fleet size (Inspector) and usage (Bedrock tokens). Monitor via [AWS Cost Explorer](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-what-is.html) with `Project=patch-automation` tag.

### EC2 Instance Requirements

For the agent to manage your EC2 instances, they need:

**Required:**
- [SSM Agent](https://docs.aws.amazon.com/systems-manager/latest/userguide/ssm-agent.html) installed and running — verify: `aws ssm describe-instance-information` shows `Online`
- IAM instance profile with [`AmazonSSMManagedInstanceCore`](https://docs.aws.amazon.com/systems-manager/latest/userguide/setup-instance-permissions.html) managed policy
- Tag: `PatchAutomation` = `enabled` — **security scope**: only instances with this tag can be patched. Configurable via `SSM_SCOPE_TAG_KEY` / `SSM_SCOPE_TAG_VALUE` in `.env`.
- Tag: `Environment` (e.g., `dev`, `staging`, `prod`) — used for environment-based routing
- Tag: `PatchGroup` (e.g., `dev-patch-group`) — used by [SSM Patch Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/patch-manager.html) for baseline association

**Recommended:**
- Tags: `SLA-CRITICAL`, `SLA-HIGH`, `SLA-MEDIUM`, `SLA-LOW` (hours) — drives SLA enforcement. Falls back to defaults (24/72/168/720h) if missing.
- IAM instance profile allows `s3:GetObject` on `s3://<your-compliance-bucket>/baseline-overrides/*` for [severity-scoped patching](docs/architecture.md#severity-scoped-patching) (the sample environment configures this automatically). The actual bucket name is `patch-compliance-reports-<account-id>`, created by `infra/lib/core-stack.ts` — substitute your hub account ID.

**Optional (audit context):**
- Tag: `ComplianceFrameworks` (e.g., `PCI-DSS,SOC2`) — included in compliance reports
- Tags: `Team`, `Product`, `Owner` — included in compliance reports for attribution

> [!TIP]
> If you already use [SSM Quick Setup](https://docs.aws.amazon.com/systems-manager/latest/userguide/quick-setup-patch.html) or [Patch Policies](https://docs.aws.amazon.com/systems-manager/latest/userguide/patch-manager-policies.html) (State Manager associations with `AWS-RunPatchBaseline`), the agent detects them automatically. When an existing Install policy covers the requested instances and the SLA window allows it, the agent defers to your policy instead of patching directly. See [Patch Policy Integration](docs/architecture.md#patch-policy-integration) for details.

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
| `BYPASS_TOOL_CONSENT` | `true` | Strands SDK tool consent. When `true`, the agent runs tools without interactive CLI prompts. **This does not bypass operator approval** -- the agent's workflow still requires human confirmation before patching (enforced by system prompt and the code-level dry-run gate). |
| `INSPECTOR_RESOURCE_TYPES` | `EC2` | Comma-separated: `EC2`, `ECR`, `LAMBDA`. Set to `EC2,ECR` for container workloads. |
| `API_KEY` / `API_KEY_OPERATOR` / `API_KEY_VIEWER` | (none) | Header-based auth (when Cognito is disabled) |
| `DEFAULT_ROLE` | `viewer` | Default role when no auth is configured. Set to `operator` for trusted networks (SSM tunnel, VPN). |
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-sonnet-5` | Override the default model |

### Commands

**Deploy** — one command handles everything (agent, infra, Explorer sync, spoke roles, UI):

```bash
./deploy.sh                   # Full deploy (~25-30 min first run)
./sample-env.sh deploy        # 15 sample EC2 instances (separate, optional)
```

When `MULTI_ACCOUNT_ENABLED=true` and spoke targets are set in `.env`, spoke role deployment is included automatically.

**Redeploy individual components** (after code changes):

| Command | What it does | Time |
|---------|-------------|------|
| `./deploy.sh agent` | Redeploy agent + update infra + rebuild UI | ~15-20 min |
| `./deploy.sh ui` | Redeploy UI only (frontend/backend changes) | ~6-8 min |

**Standalone operations:**

| Command | What it does |
|---------|-------------|
| `./deploy.sh spoke` | Deploy spoke IAM role (Patchy-SpokeIam StackSet) to spoke accounts |
| `./deploy.sh docs` | Deploy SSM Automation documents (Patchy-SsmDocs StackSet) to (hub + spokes) × all SPOKE_REGIONS |
| `./connect-ui.sh` | SSM port forward to internal ALB (when Cognito is disabled) |
| `./deploy.sh destroy` | Destroy solution infrastructure (sample env preserved) |
| `./sample-env.sh destroy` | Destroy sample environment only |

> Times vary based on network, region, and whether CDK bootstrap is required. First-time deploys are slower.

### Infrastructure Stacks

| Stack | Purpose | Always Deployed? |
|-------|---------|------------------|
| `Patchy-Network` | VPC, subnets, NAT gateways, flow logs | Yes (or `Patchy-VpcLookup` if `EXISTING_VPC_ID` set) |
| `Patchy-Core` | S3 compliance reports bucket, AgentCore IAM policy | Yes |
| `Patchy-UI` | ECS Fargate, ALB, Cognito, bastion (internal mode) | Yes |
| `Patchy-SampleEnv` | 15 EC2 instances, maintenance windows, ALBs, patch baselines | Only via `./sample-env.sh deploy` |

> [!WARNING]
> The compliance reports S3 bucket defaults to `DESTROY` with `autoDeleteObjects` for clean teardown. **For production, change `removalPolicy` to `RETAIN` in `infra/lib/core-stack.ts`** to preserve audit data if the stack is deleted.

### Using an Existing VPC

Set `EXISTING_VPC_ID` in `.env` to skip VPC creation:

```bash
EXISTING_VPC_ID=vpc-0123456789abcdef0
```

Requirements: private subnets with egress (NAT gateway), DNS hostnames and DNS support enabled.

### Tagging Convention

The agent reads [EC2 tags](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/Using_Tags.html) to determine environment, SLA, and compliance context. See [EC2 Instance Requirements](#ec2-instance-requirements) for the full tag list. Most organisations already maintain this metadata in their CMDB (ServiceNow, Device42) — if your CMDB drives EC2 tagging, the agent picks up your existing taxonomy automatically.

---

## Multi-Account Setup

Manage patching across multiple AWS accounts using [SSM Automation with TargetLocations](https://docs.aws.amazon.com/systems-manager/latest/userguide/running-automations-multiple-accounts-regions.html) and [SSM Explorer](https://docs.aws.amazon.com/systems-manager/latest/userguide/Explorer.html) for fleet discovery.

### Where does the solution run?

The solution runs in a **hub account**. You choose how to operate it:

| Deployment Model | Hub account | Who runs Quick Setup + spoke roles | Best for |
|-----------------|-------------|-----------------------------------|----------|
| **Management account** | Management account | Management account | Simple orgs, pilots |
| **Delegated administrator** | Dedicated security/tooling account | DA account (after one-time registration from management account) | Production, security-conscious orgs |

Both models use the same `./deploy.sh` — the script auto-detects whether it's running from the management account or a registered DA and adjusts accordingly.

> For the DA model, see [Delegated Administrator Deployment](docs/extending.md#delegated-administrator-deployment) for full setup instructions.

### Multi-Account Prerequisites

All prerequisites from [AWS Account Setup](#aws-account-setup) apply. Additionally:

1. **[Organizations trusted access](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/stacksets-orgs-activate-trusted-access.html)** enabled for [SSM](https://docs.aws.amazon.com/systems-manager/latest/userguide/Explorer-setup-delegated-administrator.html) and CloudFormation StackSets.

2. **Quick Setup targeting the org** — [Step 2](#aws-account-setup) must target the **entire organization or spoke OUs**. Run from the management account console, or from the DA account if [delegated administrator](docs/extending.md#delegated-administrator-deployment) is configured.

3. **Cross-account resources** — Two StackSets with separate concerns:
   - `Patchy-SpokeIam` — IAM role only (`PatchySpokeRole`), deployed to spoke accounts × primary region. IAM roles are global, so the StackSet only targets one region per spoke.
   - `Patchy-SsmDocs` — SSM Automation documents, deployed to **(hub + spokes) × all `SPOKE_REGIONS`**. Documents are regional and must exist in every (account, region) the agent fans out into.

   Deploy together with `./deploy.sh` (full deploy includes both), or independently:
   ```bash
   ./deploy.sh spoke   # IAM role only
   ./deploy.sh docs    # SSM documents only
   ```
   For non-org accounts, deploy the IAM stack directly: `npx cdk deploy Patchy-SpokeIam --profile spoke-profile -c hubAccountId=<hub-account-id>`.

4. **`deploy.sh` auto-creates the Resource Data Sync** (`patchy-fleet-sync`) in the hub region. New accounts joining the org are [automatically included](https://docs.aws.amazon.com/systems-manager/latest/userguide/Explorer-resource-data-sync-multiple-accounts-and-regions.html).

5. **[Inspector](https://docs.aws.amazon.com/inspector/latest/user/designating-admin.html)** enabled in each spoke account (or use delegated admin for org-wide coverage).

### Setup

**1. Add to `.env`:**

```bash
MULTI_ACCOUNT_ENABLED=true
SPOKE_EXECUTION_ROLE=PatchySpokeRole
AWS_ORG_ID=o-xxxxxxxxxx

# Target spoke accounts (choose one):
SPOKE_ACCOUNT_IDS=111111111111,222222222222   # Specific accounts
# SPOKE_OU_IDS=ou-abc123,ou-def456           # Or target OUs (auto-deploys to new accounts)

# Multi-region (optional, defaults to hub region):
# SPOKE_REGIONS=us-east-1,eu-west-1
```

**2. Deploy:**

```bash
./deploy.sh
```

One command handles everything — agent, infra, spoke roles, Explorer sync, and UI. Spoke role deployment is included automatically when targets are set.

> To add spoke accounts later without a full redeploy: `./deploy.sh spoke && ./deploy.sh docs`

### What Gets Deployed

| Account | Resources |
|---------|-----------|
| Hub | `Patchy-RunPatchBaseline` + `Patchy-RunRollback` Automation docs, Automation execution IAM permissions, S3 bucket policy for cross-account baseline overrides |
| Each spoke | `PatchySpokeRole` IAM role (trusts hub's `AgentCore-*` and `Patchy-UI-*` roles via `aws:PrincipalArn` condition), `Patchy-RunPatchBaseline` + `Patchy-RunRollback` Automation docs |

### Adding New Accounts

With the recommended setup (Quick Setup targeting entire org, StackSet targeting OUs, Inspector delegated admin), new accounts joining the org are handled automatically:

| Component | Auto-handles? |
|-----------|--------------|
| Quick Setup (DHMC, Host Management, Config) | Yes — auto-deploys to new accounts in targeted OUs |
| Explorer (Resource Data Sync) | Yes — new accounts auto-included |
| Agent fleet discovery | Yes — discovers accounts dynamically from Organizations |
| Spoke IAM role (Patchy-SpokeIam, SERVICE_MANAGED with OU targets) | Yes — auto-deploys to new accounts in targeted OUs |
| SSM documents (Patchy-SsmDocs, SERVICE_MANAGED with OU targets) | Yes — auto-deploys to new accounts × all SPOKE_REGIONS |
| Inspector (delegated admin) | Yes — org-wide coverage |

The only manual step for a new account: **tag instances** with `PatchAutomation=enabled` and `Environment=dev/staging/prod`.

> If using `SPOKE_ACCOUNT_IDS` (explicit list) instead of OU-based targeting, you'll need to update `.env` and run `./deploy.sh spoke && ./deploy.sh docs` to add the new account.

### How It Works

On-demand operations (scan, patch, rollback) use [SSM Automation with TargetLocations](https://docs.aws.amazon.com/systems-manager/latest/userguide/running-automations-multiple-accounts-regions.html) — one execution ID tracks the entire cross-account operation. Scheduled patching across accounts uses [Quick Setup Patch Policies](https://docs.aws.amazon.com/systems-manager/latest/userguide/patch-manager-policies.html) created from the management account — the agent defers to these for instances covered by an existing Install policy (see [Patch Policy Integration](docs/architecture.md#patch-policy-integration)).

### Example Prompts

```
Show critical vulnerabilities across all accounts
Patch all critical in prod across accounts 111111111111 and 222222222222
Check patch compliance across the production OU
What's the status of execution exec-abc123?
Roll back patches in dev across all accounts
```

---

## Security

### Network Architecture

| Component | Exposure | Access Control |
|-----------|----------|----------------|
| [ALB](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/introduction.html) | Internet-facing (Cognito mode) or internal (bastion mode) | [Cognito](https://docs.aws.amazon.com/cognito/latest/developerguide/what-is-amazon-cognito.html) authentication or [SSM Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html) port forwarding |
| [Fargate](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/AWS_Fargate.html) tasks | Private subnets only | ALB -> port 8000 only |
| EC2 instances (sample env) | Private subnets only | SSM Agent (no SSH, no open inbound ports) |
| S3 compliance bucket | Private | IAM policy (AgentCore role + Fargate task role) |

### Authentication and Authorization

| Mode | How it works | When to use |
|------|-------------|-------------|
| **[Cognito](https://docs.aws.amazon.com/cognito/latest/developerguide/what-is-amazon-cognito.html)** (default) | ALB authenticates via Cognito hosted UI. Two groups: `operators` (full access) and `viewers` (read-only dashboard). | Production / pilot |
| **API Key** | Set `API_KEY_OPERATOR` / `API_KEY_VIEWER` in `.env`. Sent via `X-API-Key` header. | Internal ALB with SSM tunnel |
| **No auth** | Defaults to `viewer` (read-only). Set `DEFAULT_ROLE=operator` in `.env` for trusted networks. | Dev/demo only |

See [Security — Authentication Modes](docs/security.md#authentication-modes) for details.

### IAM Permissions

The AgentCore runtime role (`PatchyAgentCorePolicy` in `Patchy-Core` stack) has:
- **EC2**: Read-only (`Describe*`)
- **SSM**: Read + [`SendCommand`](https://docs.aws.amazon.com/systems-manager/latest/userguide/run-command.html) + [Maintenance Window](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-maintenance.html) management (scoped to `resources: ['*']` — see note below)
- **Inspector**: Read-only ([`ListFindings`](https://docs.aws.amazon.com/inspector/latest/user/findings-managing-listing.html))
- **S3**: Read/Write on the compliance reports bucket only
- **CloudWatch**: Read-only (`DescribeAlarms`, `GetMetricData`)

> [!NOTE]
> SSM `SendCommand` is granted on `resources: ['*']`, meaning the agent can target any instance in the account. The agent enforces tag-based scoping at the application level (`PatchAutomation=enabled`), but for defense-in-depth, add an [IAM condition key](https://docs.aws.amazon.com/systems-manager/latest/userguide/security_iam_id-based-policy-examples.html) scoping to specific tags (e.g., `ssm:resourceTag/ManagedBy: IntelligentPatchAutomation`). See [Security documentation](docs/security.md) for examples.

### Data Flow

- **Within your account**: All tool calls (SSM, Inspector, EC2, S3, CloudWatch) stay within your AWS account and region.
- **Bedrock API**: Agent prompts and responses are sent to the Bedrock API endpoint in your region. Bedrock does not store or train on your data. See [Bedrock data privacy](https://docs.aws.amazon.com/bedrock/latest/userguide/data-protection.html).
- **No external services**: The solution does not call any third-party APIs.

### Safety Mechanisms

- **Steering hooks** (code-enforced): Deterministic Python hooks intercept tool calls before execution and validate workflow ordering, parameter consistency, and safety constraints. The agent cannot skip discovery before patching, proceed after a failed environment, or use mismatched severity filters — regardless of prompt manipulation. See `agent/helper/steering.py`.
- **Dry-run gate** (code-enforced): `execute_patch_operation` refuses to run without a `Scan` within the last 2 hours on every target instance. This cannot be bypassed by prompt manipulation.
- **Patch Policy awareness**: The agent checks for existing [SSM Patch Policies](https://docs.aws.amazon.com/systems-manager/latest/userguide/patch-manager-policies.html) before acting. If a scheduled Install policy already covers the target instances, the agent defers to it rather than duplicating patching. See [Patch Policy Integration](docs/architecture.md#patch-policy-integration).
- **Operator confirmation**: The agent's system prompt requires presenting dry-run results and waiting for operator approval before patching. This is prompt-enforced, not code-enforced.
- **Rate limiting**: 20 requests/minute per client IP on the API proxy.
- **Input validation**: Instance ID regex validation on all SSM operations. Session ID format validation. Message length limits (10,000 chars).
- **Per-user audit trail**: Operator identity (Cognito email) recorded in S3 reports, [SSM command comments](https://docs.aws.amazon.com/systems-manager/latest/userguide/monitor-commands.html), and CloudWatch logs.

For production hardening (IdP federation, WAF, KMS encryption, S3 Object Lock, VPC endpoints), see [Security documentation](docs/security.md).

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Agent not responding | `agentcore status` then `./deploy.sh agent` to redeploy |
| Permission errors | Check `Patchy-Core` [stack outputs](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/outputs-section-structure.html) for policy ARN |
| No instances found | Verify: (1) Explorer sync exists: `aws ssm list-resource-data-sync --sync-type SyncFromSource`, (2) Config is recording: `aws configservice describe-configuration-recorder-status`, (3) Instances have `PatchAutomation=enabled` + `Environment` tags. First-time Config setup takes up to 6 hours. |
| Spoke account instances missing from dashboard | AWS Config must be recording in the spoke account for Explorer to see its instances. Verify: assume into spoke → `aws configservice describe-configuration-recorder-status`. If not recording, enable it (Quick Setup does this automatically). Explorer takes up to 6 hours after Config activation. The chat agent finds spoke instances immediately via direct EC2 fanout — only the dashboard's Environments tab depends on Explorer. |
| Dashboard shows fewer instances than chat agent | Expected during Explorer warm-up. The dashboard Environments tab uses SSM Explorer (depends on Config + Resource Data Sync ingestion). The chat agent uses direct EC2/SSM fanout (works immediately). See [Operations — Multi-account: Explorer ingestion lag](docs/operations.md). |
| Dashboard vulnerability count differs from chat | The dashboard shows unique CVEs across all accounts (deduped by CVE ID). The chat agent shows total findings or unique CVEs depending on the query. When filtering by account, both should match. If they don't, ensure `./deploy.sh ui` was run after the latest code changes. |
| UI container unhealthy | Check [ECS logs](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/using_awslogs.html): `aws logs tail /patch-automation/ui --follow` |
| SSM tunnel fails | Ensure [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) is installed and at least one instance is `Online` |
| Cognito login redirect error | Redeploy: `./deploy.sh ui` (fixes callback URL case mismatch) |
| Cognito users gone after redeploy | User Pool was recreated (new Pool ID). Recreate users — see [Operations](docs/operations.md) |
| Self-signed cert warning | Expected with default setup. Click through. For production, use [ACM public certificate](https://docs.aws.amazon.com/acm/latest/userguide/gs.html) |
| Agent asks for info already provided | Click "New conversation" to reset session. If persistent, check memory logs: `aws logs tail /aws/bedrock-agentcore/runtimes/<agent-id>-DEFAULT --since 5m` and look for `[MEMORY]` entries |
| Stale config after account switch | Delete `agent/agentcore/.cli/deployed-state.json` and redeploy: `./deploy.sh agent` |
| Bedrock model not accessible | Verify model access is enabled: Console → Bedrock → Model access. Must be enabled in your deployment region. |
| Config not recording (Explorer empty after 6+ hours) | Check Config recorder: `aws configservice describe-configuration-recorder-status`. Verify role trust includes `config.amazonaws.com`. See [Operations — Verifying Fleet Discovery](docs/operations.md#verifying-fleet-discovery). |
| VPC deployment fails | VPC needs at least 2 AZs with private subnets + NAT gateway. If using `EXISTING_VPC_ID`, ensure DNS hostnames and DNS support are enabled. |
| ALB fails: "multiple subnets in same AZ" | Your VPC has duplicate subnets per AZ. Add `vpcSubnets: { onePerAz: true }` to the ALB construct. |

### Deploying to a New Account

1. Complete the [Prerequisites](#prerequisites) in the new account (Quick Setup, Explorer, Inspector)
2. Update `.env` with new `AWS_PROFILE` (and optionally `AWS_REGION`)
3. Run `./deploy.sh` (and optionally `./sample-env.sh deploy`)

> If switching FROM an existing deployment, also delete `agent/agentcore/.cli/deployed-state.json` (contains the previous account's agent ARN).

---

## Testing & Evaluation

### Unit Tests (deterministic, no AWS calls)

```bash
# Steering logic, tool validation, error handling
python3 -m pytest tests/test_tools_unit.py -v
python3 -m pytest tests/test_steering.py -v
```

### Tool Selection Eval (LLM-in-the-loop, requires Bedrock)

```bash
# Run eval against Bedrock and compare to baseline
python3 agent/eval/run_eval.py

# Update baseline after intentional prompt/docstring changes
python3 agent/eval/run_eval.py --update-baseline

# Deploy gate: automatically runs before deploy (skip with SKIP_EVAL=true)
./deploy.sh agent
```

The eval sends 20 scenarios to the model with tool schemas and validates correct tool selection + response content assertions. Costs ~$0.05 per run. See `agent/eval/README.md` for details.

### Integration Tests (requires deployed agent + AWS)

```bash
# Smoke test against deployed AgentCore runtime
python3 tests/smoke_test.py
```

---

## Detailed Documentation

| Document | What it covers |
|----------|---------------|
| [Architecture](docs/architecture.md) | Unified agent design, memory model, SLA decision flow, severity-scoped patching, rollback verification, patch policy integration, compliance reporting |
| [Operations](docs/operations.md) | User management, long-term memory, guardrails, SLA configuration, maintenance windows, web UI access, local development |
| [Observability](docs/observability.md) | 4-layer observability model, GenAI dashboard, tool log queries, full conversation replay, SSM/S3 ground truth, debugging workflows |
| [Security](docs/security.md) | Production hardening, IdP federation, IAM session tags, WAF, KMS, S3 Object Lock, VPC endpoints, network architecture |
| [Extending the Solution](docs/extending.md) | Adding tools, multi-OS support, CMDB integration, change management, third-party scanners, Slack/Teams, cross-account |

---

## Cost Estimate

Monthly costs vary by configuration. Typical single-account deployment (us-east-1):

| Resource | Estimated Cost |
|----------|---------------|
| NAT Gateway (1-2) | $32-64 |
| Fargate (UI task) | $30-50 |
| Application Load Balancer | $16-25 |
| S3 (compliance reports) | < $1 |
| Bedrock model invocations | Usage-based |
| Sample environment (optional) | $60 (15 instances + ALBs) |

Charges begin immediately on deployment and stop when stacks are deleted.

## Teardown

> [!WARNING]
> `./deploy.sh destroy` permanently deletes the S3 compliance reports bucket and all audit data (default configuration). To retain reports before destroying, either change `removalPolicy` to `RETAIN` in `core-stack.ts` or copy the bucket contents to another location.

```bash
# 1. Remove sample environment (if deployed)
./sample-env.sh destroy

# 2. Remove solution infrastructure (prompts for confirmation)
./deploy.sh destroy

# 3. Remove AgentCore agent runtime
agentcore remove all -y

# 4. (Optional) Remove Bedrock guardrail if created
aws bedrock delete-guardrail --guardrail-identifier <guardrail-id>

# 5. (Optional) Remove TLS certificate if auto-generated
aws acm delete-certificate --certificate-arn <cert-arn>
```

> [!NOTE]
> - `./deploy.sh destroy` preserves the Patchy-SpokeIam + Patchy-SsmDocs StackSets and the Resource Data Sync by default. To also remove these: `DESTROY_SPOKE_STACKSET=true DESTROY_FLEET_SYNC=true ./deploy.sh destroy`. Or remove individually: `./deploy.sh destroy --spoke-only` (IAM) or `./deploy.sh destroy --docs-only` (SSM docs).
> - `agentcore destroy` removes the agent runtime but leaves the memory resource intact (may contain audit-relevant history). To delete memory: `aws bedrock-agentcore-control delete-memory --memory-id <memory-id>`
> - NAT Gateway and Fargate charges stop immediately on stack deletion. CloudWatch Logs are retained for 14 days (configured retention).

---

## Getting Help

- **Issues**: Report bugs or request features on the project's issue tracker
- **Logs**: Start with the [Observability guide](docs/observability.md) -- Layer 1 (Generative AI Dashboard) answers most "what happened?" questions
- **Deployment issues**: Check [Troubleshooting](#troubleshooting) above, then review `deploy.sh` output for error messages
- **Security questions**: See [Security documentation](docs/security.md) for IAM scoping, network architecture, and hardening
