# Operations — Intelligent Patch Automation

Post-deployment setup, configuration, and day-to-day running.

## Contents

- [Environment Configuration (`.env`)](#environment-configuration-env)
- [Operator Command Reference](#operator-command-reference)
- [Tagging Your Fleet](#tagging-your-fleet)
- [SLA Configuration](#sla-configuration)
- [User Management](#user-management)
- [Bedrock Guardrails (Optional)](#bedrock-guardrails-optional)

---

## Environment Configuration (`.env`)

All configuration lives in a single `.env` file. Copy the template and fill it in before your first deploy:

```bash
cp .env.example .env
```

For most single-account deployments you only need to set two values — `AWS_PROFILE` and `AWS_REGION`. Everything else has a sensible default. The sections below group the variables by what they do, so you can skim to the ones that matter for your setup. Anything not listed here can stay commented out.

### Required

- `AWS_PROFILE` (default `default`) — the AWS CLI named profile to deploy with. Its credentials need permissions for Bedrock, SSM, EC2, S3, ECS, and Inspector.
- `AWS_REGION` (default `us-east-1`) — the target region. The solution is region-agnostic; it runs wherever these services are enabled.

### Agent

- `AGENT_NAME` (default `patchy`) — name of the AgentCore runtime.
- `BYPASS_TOOL_CONSENT` (default `true`) — skips the per-tool consent prompt. Leave `true` for autonomous operation.
- `BEDROCK_MODEL_ID` (default `us.anthropic.claude-sonnet-5`) — override to run a different Bedrock model.
- `PROJECT_NAME` (default `patchy`) — used to derive CloudFormation stack names (`AgentCore-<PROJECT_NAME>-default`). Only change it if you're running more than one instance of the solution in the same account.
- `MAX_FLEET_SIZE` (default `5000`) — soft cap on how many EC2 instances the fleet cache enriches per refresh. Raise it for larger fleets.

### Instance scope (security / blast radius)

- `SSM_SCOPE_TAG_KEY` (default `PatchAutomation`) — tag key that marks an instance as in-scope. The agent, and the IAM policy, refuse to touch anything without it.
- `SSM_SCOPE_TAG_VALUE` (default `enabled`) — required value for the scope tag.
- `INSPECTOR_RESOURCE_TYPES` (default `EC2`) — which resource types Inspector scans. Comma-separated: `EC2`, `ECR`, `LAMBDA`. Add `ECR` for container image findings.

### Web UI authentication

- `DEFAULT_ROLE` (default `viewer`) — role assigned when no auth is configured. Set to `operator` only for a trusted pilot/tunnel setup where everyone should have full access.
- `API_KEY` (unset) — enables API-key auth on the UI. When set alone, it grants operator role (backward compatible).
- `API_KEY_OPERATOR` (unset) — explicit operator key: full access (dashboard + chat + patching).
- `API_KEY_VIEWER` (unset) — viewer key: read-only (dashboard + health, no chat).

With no Cognito and no API keys set, auth is disabled and everyone defaults to `DEFAULT_ROLE`. See [Security — Authentication Modes](security.md#authentication-modes) for the full picture.

### TLS and Cognito

- `ACM_CERTIFICATE_ARN` (auto) — ACM certificate for the ALB. `deploy.sh` runs `setup-tls.sh` to generate a self-signed cert if this is unset. For production, point it at a public ACM cert.
- `COGNITO_ENABLED` (default `true` when a cert is set) — when enabled, the ALB is internet-facing and requires Cognito login. Set `false` to use an internal ALB + bastion host instead.
- `COGNITO_DOMAIN_PREFIX` (default `patchy-<account_id>`) — hosted-UI domain prefix. Override only if you need a custom, globally unique prefix.

### Container runtime

- `CDK_DOCKER` (auto) — CDK builds the UI container image locally with Docker or Finch. `deploy.sh` auto-detects Finch; set `CDK_DOCKER=finch` to force it.
- `EXISTING_VPC_ID` (unset) — use your own VPC instead of creating one. Needs private subnets with NAT (`PRIVATE_WITH_EGRESS`) for Fargate. When set, the `Patchy-Network` stack is skipped.

### Multi-account (hub-and-spoke)

Only relevant if you're patching across more than one AWS account. See [Multi-account setup](../README.md#multi-account-setup) for the full walkthrough.

- `MULTI_ACCOUNT_ENABLED` (unset) — set `true` to turn on cross-account operations via STS assume-role and SSM `TargetLocations`.
- `AWS_ORG_ID` (unset) — required when multi-account is on. Scopes the cross-account assume-role and S3 bucket policies to your org (`aws:PrincipalOrgID`).
- `SPOKE_EXECUTION_ROLE` (default `PatchySpokeRole`) — name of the IAM role deployed into each spoke account.
- `SPOKE_ACCOUNT_IDS` (unset) — comma-separated explicit account list (self-managed StackSet).
- `SPOKE_OU_IDS` (unset) — OU-based targeting (service-managed StackSet); auto-enrols new accounts joining the OU. Use this or `SPOKE_ACCOUNT_IDS`, not both.
- `SPOKE_REGIONS` (default `AWS_REGION`) — comma-separated regions to fan out into. Inspector must be enabled in each.
- `SAMPLE_ENV_ACCOUNTS` (default: first spoke) — accounts that `./sample-env.sh` deploys demo EC2 instances into.

### Observability

- `ENABLE_RUNTIME_LOGS` (off) — delivers the runtime's stdout/stderr to CloudWatch Logs with 14-day retention.
- `ENABLE_TRACING` (off) — enables account-level Transaction Search and per-runtime trace spans, required to populate the GenAI Observability dashboard. Spans take ~10 min to appear.

See [`docs/observability.md`](observability.md) for what these unlock.

### Guardrails

- `GUARDRAIL_ID` (unset) — Bedrock guardrail to apply. Run `python agent/setup_guardrail.py` to create a sample one; it prints the values to set here.
- `GUARDRAIL_VERSION` (unset) — version of the guardrail above.

See [Bedrock Guardrails](#bedrock-guardrails-optional).

### Teardown gates (opt-in)

These protect resources with org-wide blast radius. They're preserved by default on `./deploy.sh destroy`. See `./deploy.sh destroy --help`.

- `DESTROY_SPOKE_STACKSET` (default `false`) — also destroys the `Patchy-SpokeRole` StackSet across every spoke account/region.
- `DESTROY_FLEET_SYNC` (default `false`) — also deletes the `patchy-fleet-sync` Resource Data Sync. Re-creating it triggers a multi-hour ingestion window.

---

## Operator Command Reference

These are the prompt patterns the system is tuned for. Stick close to them and you'll get the most reliable results.

### Workflows

Critical vulnerability response:

| Step | Prompt | What happens |
|------|--------|-------------|
| 1 | `Show CRITICAL vulnerabilities in prod` | CVEs affecting production, CVSS scores, instance count |
| 2 | `How widespread is CVE-2026-33811?` | Fleet impact — environments, SLA deadlines |
| 3 | `Patch CVE-2026-33811 in prod` | Scope → SLA decision → execute |
| 4 | `Check status` | Automation progress per account |
| 5 | `Verify health` | SSM connectivity + CloudWatch alarms |

Staged rollout: `Patch CVE-X in dev` → `Check status` → `Patch CVE-X in staging` → `Check status` → `Patch CVE-X in prod`

Routine compliance: `Preview patches in prod` → `Check status` → `Patch all prod` → `Verify health`

### Command Patterns

| Category | Prompt | Scope |
|----------|--------|-------|
| Discovery | `Show instances in dev` | All managed dev instances |
| | `Show vulnerabilities in staging` | Active CVEs in staging |
| | `What's the patch compliance for prod?` | Missing patch counts |
| Preview | `Preview patches in prod` | Dry-run, no changes |
| | `Dry-run on i-xxx, i-yyy` | Specific instances only |
| Patch | `Patch CVE-2026-6772 in dev` | Severity-scoped |
| | `Patch all prod` | All severities |
| | `Patch HIGH severity in staging` | HIGH+ only |
| | `Patch instance i-0ea66eea856a9028d` | Single instance |
| Rollback | `Rollback i-0ea66eea856a9028d` | Single instance |
| | `Rollback dev patches` | Environment-wide |
| Status | `Check status` | Last automation |
| | `Check status of execution f5da8b53-...` | Specific execution |
| | `Verify health` | SSM + CloudWatch post-patch |
| Compliance | `Show SLA breaches this week` | Violations only |
| | `Show compliance reports` | Recent reports from S3 |
| Windows | `Show maintenance windows for prod` | Configured schedules |
| Emergency | `Emergency stop` | Cancel active automation |

### Tips

- Name the environment — "in dev", "in prod", "in staging".
- Include account IDs when you need them — "in account 111122223333".
- Use CVE IDs for targeted patching — "Patch CVE-X in dev".
- "Preview" reads only; "Patch" executes.

---

## Tagging Your Fleet

The agent's behaviour is driven entirely by EC2 instance tags. No code changes and no config files — set the tags and the agent works from them.

How the agent reads them depends on what it's doing:

- Scoping and SLA decisions read tags straight from EC2 `describe_instances` at request time. Change a tag value (say, bump `SLA-HIGH` from 72 to 24) and the next request sees it — no redeploy, no waiting.
- Fleet discovery — which instances exist, and the dashboard's environment view — flows through SSM Explorer, which has propagation lag (minutes to hours, and up to 6 hours the first time AWS Config is activated). A newly launched instance won't show up until Explorer ingests it, even though its tags are already correct.

In practice: existing instances react to tag changes immediately, but a brand-new instance takes a beat to become visible. See [`docs/architecture.md`](architecture.md#fleet-discovery) for the two discovery paths in detail.

### Required tags (the instance is invisible without them)

| Tag | Required value | Purpose |
|-----|---------------|---------|
| `PatchAutomation` | `enabled` | Security scope. The agent won't touch any instance that lacks this tag. Configurable via `SSM_SCOPE_TAG_KEY` / `SSM_SCOPE_TAG_VALUE` in `.env`. |
| `Environment` | `dev`, `staging`, or `prod` | Routing label. Decides which fleet bucket the instance lands in for "patch all dev" style queries. |

Without these two, the instance is skipped by every patch operation — compliance, dry-run, install, rollback, all of it.

### SLA tags (drive the EMERGENCY vs SCHEDULED decision)

| Tag | Example | Meaning |
|-----|---------|---------|
| `SLA-CRITICAL` | `6` | Patch CRITICAL CVEs within 6 hours |
| `SLA-HIGH` | `24` | Patch HIGH CVEs within 24 hours |
| `SLA-MEDIUM` | `168` | Patch MEDIUM CVEs within 168 hours (7 days) |
| `SLA-LOW` | `720` | Patch LOW CVEs within 720 hours (30 days) |

If a tag is missing, the agent falls back to global defaults (24h / 72h / 168h / 720h), defined in `agent/helper/tools/_shared.py` (`_DEFAULT_SLA`). It reads the tag matching the CVE's severity, compares it against the next maintenance window, and routes EMERGENCY (immediate) or SCHEDULED (defer to the existing patch policy).

### Audit and attribution tags (recommended)

These don't change any decision, but they populate the compliance reports and dashboard.

| Tag | Example | Where it appears |
|-----|---------|-----------------|
| `ComplianceFrameworks` | `PCI-DSS,SOC2` | Compliance dashboard "Framework" column. Comma-separated. Optional — the solution has no built-in framework knowledge; this is just audit context that tells auditors why the SLA was set. |
| `Team` | `platform` | Team breakdown in compliance reports. Lets you see SLA met/breached counts per team. |
| `Product` | `api-gateway` | Audit trail attribution. |
| `Owner` | `platform-lead@example.com` | Audit trail attribution. |
| `PatchGroup` | `dev-patch-group` | Used by [SSM Patch Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/patch-manager.html) for baseline association. Required if you use SSM Patch Policies for SCHEDULED patching. |

For tag-related issues, see [`docs/troubleshooting.md`](troubleshooting.md#what-happens-when-tags-are-missing).

---

## SLA Configuration

SLA thresholds come entirely from EC2 instance tags — the solution has no built-in compliance framework knowledge.

| Tag | Example | Meaning |
|-----|---------|---------|
| `SLA-CRITICAL` | `12` | Patch within 12 hours |
| `SLA-HIGH` | `48` | Patch within 48 hours |
| `SLA-MEDIUM` | `168` | Patch within 168 hours (7 days) |
| `SLA-LOW` | `720` | Patch within 720 hours (30 days) |

Missing tags fall back to defaults (24/72/168/720h), defined in `agent/helper/tools/_shared.py` (`_DEFAULT_SLA`).

The `ComplianceFrameworks` tag (for example `PCI-DSS,SOC2`) is optional. It's included in compliance reports for audit context — it tells auditors why a given SLA was set — but it doesn't drive the SLA calculation.

Sample environment SLA configuration:

| Environment | Framework | CRITICAL | HIGH | MEDIUM | LOW |
|-------------|-----------|----------|------|--------|-----|
| dev (all) | SOC2 | 24h | 72h | 168h | 720h |
| staging (PCI-DSS) | PCI-DSS | 12h | 48h | 168h | 720h |
| staging (others) | SOC2 | 24h | 72h | 168h | 720h |
| prod (PCI-DSS) | PCI-DSS | 6h | 24h | 168h | 720h |
| prod (others) | SOC2/HIPAA | 24h | 72h | 168h | 720h |

> Enterprise tip: push SLA hours to EC2 tags via AWS Organizations tag policies, Service Catalog, or your CI/CD pipeline. The agent reads them at runtime, so you don't need to redeploy when thresholds change.

---

## User Management

The web UI makes users sign in through Amazon Cognito. The deploy script creates the Cognito User Pool and two permission groups for you — all you have to do is create the actual user accounts.

### Roles

| Role | Group name | What the user can do |
|------|-----------|---------------------|
| Operator | `operators` | Full access — dashboard, chat, patch execution, compliance reports |
| Viewer | `viewers` | Read-only — dashboard and compliance reports only, no chat or patching |

Every user needs to belong to at least one group.

### Quick method: interactive CLI

The easiest way to create a user:

```bash
./deploy.sh create-user
```

It prompts for email, password, and role, then creates the user with a temporary password and drops them in the right group. On first login, Cognito asks the user to set a permanent password.

### Manual method: AWS CLI

Use this when you need scripted or automated provisioning.

Step 1 — get the User Pool ID from the deployed Patchy-UI stack outputs:

```bash
POOL_ID=$(aws cloudformation describe-stacks --stack-name Patchy-UI \
  --query 'Stacks[0].Outputs[?OutputKey==`CognitoUserPoolId`].OutputValue' --output text)
```

Step 2 — create the user:

```bash
aws cognito-idp admin-create-user --user-pool-id $POOL_ID \
  --username user@example.com \
  --user-attributes Name=email,Value=user@example.com Name=email_verified,Value=true \
  --temporary-password '<password>!' --message-action SUPPRESS
```

`--message-action SUPPRESS` stops Cognito from sending a welcome email. Drop it if you want the user to get their temporary password by email.

Step 3 — set a permanent password (skips the forced-change-on-first-login flow):

```bash
aws cognito-idp admin-set-user-password --user-pool-id $POOL_ID \
  --username user@example.com --password '<PermanentPassword>!' --permanent
```

Password rules: at least 8 characters, with an uppercase letter, a lowercase letter, and a number.

Step 4 — assign the user to a role:

```bash
# For operator access:
aws cognito-idp admin-add-user-to-group --user-pool-id $POOL_ID \
  --username user@example.com --group-name operators

# For viewer access:
aws cognito-idp admin-add-user-to-group --user-pool-id $POOL_ID \
  --username user@example.com --group-name viewers
```

### Verifying users

```bash
# List all users in the pool
aws cognito-idp list-users --user-pool-id $POOL_ID --output table

# Check which group a specific user belongs to
aws cognito-idp admin-list-groups-for-user --user-pool-id $POOL_ID --username user@example.com
```

### Removing a user

```bash
aws cognito-idp admin-delete-user --user-pool-id $POOL_ID --username user@example.com
```

### Worth knowing

- Recreating the User Pool destroys all users. If a CDK change forces the Cognito User Pool to be replaced (a replacement, not an update), every existing user is gone for good. Always check the CloudFormation changeset before deploying CDK changes that touch the UI stack.
- Federated identity: in production, where you already manage users through an identity provider (Okta, Azure AD, Ping, AWS IAM Identity Center), federate the Cognito User Pool via OIDC or SAML and map your IdP groups to `operators` and `viewers`. See [Security — Authentication Modes](security.md#authentication-modes).

---

## Bedrock Guardrails (Optional)

Creates a sample guardrail with topic filtering, content safety, and sensitive data masking.

```bash
source venv/bin/activate
python agent/setup_guardrail.py
```

Add the output values to `.env` and redeploy:

```bash
GUARDRAIL_ID=<guardrail-id-from-output>
GUARDRAIL_VERSION=<version-from-output>
```

```bash
./deploy.sh agent
```

The sample guardrail includes:

- A denied topic covering non-patching queries (general knowledge, unrelated AWS services)
- Content filters that block harmful content at the HIGH threshold
- Sensitive-data handling that anonymizes AWS account IDs and IP addresses in responses
- Prompt-attack detection that blocks prompt injection attempts

Extend it in the Bedrock console or via the API with whatever additional policies your compliance requirements call for.
