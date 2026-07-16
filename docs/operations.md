# Operations — Intelligent Patch Automation

Post-deployment setup, configuration, and day-to-day operations.

---

## Verifying Fleet Discovery

The agent discovers instances through [AWS Systems Manager (SSM) Explorer](https://docs.aws.amazon.com/systems-manager/latest/userguide/Explorer.html). After deployment, verify the pipeline is working:

```bash
# Check the Resource Data Sync exists
aws ssm list-resource-data-sync --sync-type SyncFromSource \
  --query 'ResourceDataSyncItems[].SyncName'

# Query Explorer for instances (should return your fleet)
aws ssm get-ops-summary --sync-name patchy-fleet-sync \
  --result-attributes TypeName=AWS:EC2InstanceInformation --max-results 5
```

**If Explorer returns 0 instances:**

| Check | Command | Expected |
|-------|---------|----------|
| Config recording | `aws configservice describe-configuration-recorder-status` | `lastStatus: SUCCESS`, `recording: true` |
| Config discovered EC2 | `aws configservice get-discovered-resource-counts --resource-types AWS::EC2::Instance` | Count > 0 |
| SSM Agent online | `aws ssm describe-instance-information --query 'length(InstanceInformationList)'` | Count > 0 |
| Instance tags | `aws ec2 describe-instances --filters Name=tag:PatchAutomation,Values=enabled --query 'Reservations[].Instances[].InstanceId'` | Your instance IDs |

Config takes up to 6 hours to populate on first activation. If Config is recording but Explorer is empty, wait and recheck.

**Multi-account: Explorer ingestion lag**

When deploying to spoke accounts, newly launched instances may take 15-60 minutes to appear in the `patchy-fleet-sync` Resource Data Sync. During this window:

| Surface | Data source | Sees new spoke instances? |
|---------|-------------|--------------------------|
| Dashboard — Environments tab | Explorer (Resource Data Sync) | ❌ Not until ingested |
| Dashboard — Vulnerabilities tab | Inspector (direct fanout) | ✅ Immediately (after Inspector scans, ~15 min) |
| Chat — `get_fleet_overview` | Explorer | ❌ Not until ingested |
| Chat — `get_vulnerability_findings` | Inspector (direct fanout) | ✅ Immediately |
| Chat — `get_patch_compliance` | EC2 + SSM (direct fanout) | ✅ Immediately |

This means the chat agent can find and patch spoke instances before the dashboard's Environments tab shows them. This is expected behavior — not a bug. The dashboard's "warming up" banner only appears when Explorer returns **zero** entities (fresh sync). Once the hub's instances are ingested, the banner disappears even if spoke instances are still pending.

**To verify spoke ingestion progress:**
```bash
aws ssm get-ops-summary --sync-name patchy-fleet-sync \
  --result-attributes TypeName=AWS:EC2InstanceInformation \
  --filters "Key=AWS:EC2InstanceInformation.SourceAccountId,Values=<spoke-account-id>,Type=Equal"
```

Empty response = not yet ingested. Wait for the spoke's `Patchy-SampleEnv-Inventory` association to run (every 30 minutes).

---

## User Management

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
  --temporary-password 'TempPass1!' --message-action SUPPRESS

# Set a permanent password (bypasses the forced-change-on-first-login flow)
aws cognito-idp admin-set-user-password --user-pool-id $POOL_ID \
  --username user@example.com --password 'YourSecurePassword1!' --permanent
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

**List users and verify group membership:**
```bash
aws cognito-idp list-users --user-pool-id $POOL_ID --output table
aws cognito-idp admin-list-groups-for-user --user-pool-id $POOL_ID --username user@example.com
```

Two groups are created automatically by the CDK stack:
- `operators` -- full access: dashboard, chat, patch execution, compliance reports
- `viewers` -- read-only: dashboard and compliance reports only

> [!WARNING]
> If the Cognito User Pool is recreated (e.g., after a CDK change that replaces the pool resource), **all users are lost**. The Pool ID is in the CloudFormation outputs -- always verify it before creating users. To federate with your corporate IdP instead of managing users manually, see [Security — Authentication Modes](security.md#authentication-modes).

---

## Memory

The agent uses **short-term memory (STM)** to persist conversation turns within a session. Sessions survive page refreshes — the frontend stores the `session_id` and current Cognito email in localStorage, and AgentCore stores the conversation server-side.

On page refresh the chat panel hydrates from `GET /api/session/{session_id}/messages`, which reads the same Memory store via `MemoryClient.get_last_k_turns`. The visible chat and the agent's context are always in sync. Sign-out clears the localStorage keys; signing in as a different user is detected by comparing the stored email to the current Cognito JWT and starts a fresh session.

**Long-term memory (LTM)** extraction is configured (AgentCore extracts semantic facts and session summaries), but **LTM retrieval is intentionally disabled**. Retrieving LTM caused contamination — stale command IDs and instance lists from previous sessions were injected into new conversations, leading to incorrect agent behaviour.

Memories are scoped per operator via `actor_id` — Operator A's sessions are invisible to Operator B.

---

## Bedrock Guardrails (Optional)

Creates a sample guardrail with topic filtering, content safety, and sensitive data masking.

```bash
source venv/bin/activate
python agent/setup_guardrail.py
```

Then add the output values to `.env` and redeploy:
```bash
GUARDRAIL_ID=<guardrail-id-from-output>
GUARDRAIL_VERSION=<version-from-output>
```

```bash
./deploy.sh agent
```

The sample guardrail includes:
- Denied topic: non-patching queries (general knowledge, unrelated AWS services)
- Content filters: blocks harmful content at HIGH threshold
- Sensitive data: anonymizes AWS account IDs and IP addresses in responses
- Prompt attack detection: blocks prompt injection attempts

Extend it in the Bedrock console or via API with additional policies for your compliance requirements.

---

## Tagging Your Fleet

The agent's behaviour is driven entirely by EC2 instance tags. No code changes, no config files, no per-instance API calls — set the tags once and the agent picks them up at runtime. Most organisations already keep this metadata in their CMDB; if your CMDB drives EC2 tagging, the agent inherits your taxonomy automatically.

### Required tags (instance is invisible without these)

| Tag | Required value | Purpose |
|-----|---------------|---------|
| `PatchAutomation` | `enabled` | Security scope. The agent refuses to touch any instance without this tag. Configurable via `SSM_SCOPE_TAG_KEY` / `SSM_SCOPE_TAG_VALUE` in `.env`. |
| `Environment` | `dev`, `staging`, or `prod` | Routing label. Determines which fleet bucket the instance lands in for "patch all dev" type queries. |

Without these, the instance is excluded from every patch operation. Compliance, dry-run, install, rollback — all skip it.

### SLA tags (drive the EMERGENCY vs SCHEDULED decision)

| Tag | Example | Meaning |
|-----|---------|---------|
| `SLA-CRITICAL` | `6` | Patch CRITICAL CVEs within 6 hours |
| `SLA-HIGH` | `24` | Patch HIGH CVEs within 24 hours |
| `SLA-MEDIUM` | `168` | Patch MEDIUM CVEs within 168 hours (7 days) |
| `SLA-LOW` | `720` | Patch LOW CVEs within 720 hours (30 days) |

Falls back to global defaults (24h / 72h / 168h / 720h) if any tag is missing. The agent reads the tag matching the CVE's severity, compares against the next maintenance window, and routes EMERGENCY (immediate) or SCHEDULED (defer to existing patch policy) accordingly.

### Audit and attribution tags (recommended)

These don't drive any decision but populate the compliance reports and dashboard.

| Tag | Example | Where it appears |
|-----|---------|-----------------|
| `ComplianceFrameworks` | `PCI-DSS,SOC2` | Compliance dashboard "Framework" column. Comma-separated list. **Optional** — the solution has no built-in framework knowledge; this is just audit context that tells auditors *why* the SLA was set. |
| `Team` | `platform` | Compliance reports' team breakdown. Lets you see SLA met / breached counts per team. |
| `Product` | `api-gateway` | Audit trail attribution. |
| `Owner` | `platform-lead@example.com` | Audit trail attribution. |
| `PatchGroup` | `dev-patch-group` | Used by [SSM Patch Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/patch-manager.html) for baseline association. Required if you use SSM Patch Policies for SCHEDULED patching. |

### Quick-start: tag a single instance

```bash
aws ec2 create-tags --resources i-0123456789abcdef0 \
  --tags \
    Key=PatchAutomation,Value=enabled \
    Key=Environment,Value=staging \
    Key=PatchGroup,Value=staging-patch-group \
    Key=SLA-CRITICAL,Value=12 \
    Key=SLA-HIGH,Value=48 \
    Key=SLA-MEDIUM,Value=168 \
    Key=SLA-LOW,Value=720 \
    Key=ComplianceFrameworks,Value=PCI-DSS,SOC2 \
    Key=Team,Value=platform \
    Key=Product,Value=api-gateway \
    Key=Owner,Value=platform-lead@example.com
```

### Quick-start: tag every instance in an environment

```bash
# Add ComplianceFrameworks=PCI-DSS to every staging instance in this account
aws ec2 describe-instances \
  --filters "Name=tag:Environment,Values=staging" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].InstanceId' --output text \
| xargs -n1 -I{} aws ec2 create-tags \
    --resources {} \
    --tags Key=ComplianceFrameworks,Value=PCI-DSS
```

### What happens when tags are missing

| Missing tag | Effect |
|-------------|--------|
| `PatchAutomation=enabled` | Instance is excluded from every operation. Path A patches refuse with `InstanceOutOfScope`. |
| `Environment` | Instance shows up in the fleet but Path B (tag-based) operations don't target it for any specific environment. |
| `SLA-<severity>` | Falls back to global default (24/72/168/720h). Compliance report records `sla_source: default` so auditors see the decision was made without an explicit policy. |
| `ComplianceFrameworks` | Compliance dashboard's Framework column is blank for that report. SLA decision is unaffected. |
| `Team` / `Product` / `Owner` | Recorded as `unknown`. Compliance breakdowns by team show an `unknown` bucket. |

### Where these tags come from in production

- **AWS Organizations tag policies** — enforce a required set of tags org-wide.
- **Service Catalog / CloudFormation / CDK** — bake tags into the launch template so new instances arrive correctly tagged.
- **Lambda + EventBridge** — listen for `RunInstances` events and apply tags from your CMDB at launch time.
- **CMDB sync** — periodic job that reads ServiceNow / Device42 / etc. and reconciles EC2 tags.

The solution doesn't care which path you choose — it reads tags at runtime, so updates propagate within the next fleet-cache TTL (5 minutes).

> The sample environment (`./sample-env.sh deploy`) creates instances with all of the above tags pre-populated, including realistic per-environment compliance framework / SLA combinations. See `infra/lib/sample/sample-environment-stack.ts` for the exact values.

---

## SLA Configuration

SLA thresholds come entirely from EC2 instance tags -- the solution has no built-in compliance framework knowledge.

| Tag | Example | Meaning |
|-----|---------|---------|
| `SLA-CRITICAL` | `12` | Patch within 12 hours |
| `SLA-HIGH` | `48` | Patch within 48 hours |
| `SLA-MEDIUM` | `168` | Patch within 168 hours (7 days) |
| `SLA-LOW` | `720` | Patch within 720 hours (30 days) |

Falls back to defaults (24/72/168/720h) if tags are missing.

The `ComplianceFrameworks` tag (e.g., `PCI-DSS,SOC2`) is optional -- included in compliance reports for audit context (tells auditors *why* the SLA was set) but does not drive SLA calculation.

**Sample environment SLA configuration:**

| Environment | Framework | CRITICAL | HIGH | MEDIUM | LOW |
|-------------|-----------|----------|------|--------|-----|
| dev (all) | SOC2 | 24h | 72h | 168h | 720h |
| staging (PCI-DSS) | PCI-DSS | 12h | 48h | 168h | 720h |
| staging (others) | SOC2 | 24h | 72h | 168h | 720h |
| prod (PCI-DSS) | PCI-DSS | 6h | 24h | 168h | 720h |
| prod (others) | SOC2/HIPAA | 24h | 72h | 168h | 720h |

> **Enterprise tip**: Push SLA hours to EC2 tags via AWS Organizations tag policies, Service Catalog, or your CI/CD pipeline. The agent reads them at runtime -- no redeployment needed when thresholds change.

---

## Maintenance Windows

The agent uses SSM maintenance windows to decide patching mode:
- **SCHEDULED**: next window is within the SLA deadline -> wait for it
- **EMERGENCY**: next window exceeds the SLA deadline -> patch immediately

Sample windows (from `./sample-env.sh deploy`): dev daily 1AM UTC, staging Tuesdays 2AM UTC, prod 1st of month 2AM UTC.

> **Using a change management system?** Extend the agent to query your change calendar (ServiceNow, Jira, PagerDuty) instead of or alongside SSM windows. The SLA-vs-window logic just needs a "next available window" timestamp -- the source doesn't matter.

---

## Patch Policy Setup (Recommended)

For scheduled patching, the agent checks for existing SSM Patch Policy associations (State Manager). If your instances don't have patch policies configured:
- The agent will warn: "No patch policy found for these instances"
- On-demand patching still works via direct `send_command`
- To set up: Console -> Systems Manager -> Quick Setup -> Patch Policy

The sample environment creates maintenance windows but not patch policy associations. In production, configure patch policies via [Quick Setup](https://docs.aws.amazon.com/systems-manager/latest/userguide/quick-setup-patch-manager.html) for consistent scheduled patching.

---

## Web UI

Browser-based operations console with two panels:

- **Dashboard**: 4-metric KPI strip (fleet health, critical CVEs, patch compliance with radial gauge, SLA compliance with radial gauge), environment cards with quick actions (scan/patch/report), vulnerability table, compliance summary, decision audit trail
- **Chat**: Streaming agent responses with workflow stepper, syntax-highlighted code blocks, copy-to-clipboard, confirmation dialogs for destructive actions, command palette (Cmd+K), conversation rehydration from AgentCore Memory on page refresh

### Accessing the UI

The URL is printed at the end of `deploy.sh` and available in CloudFormation outputs:

```bash
aws cloudformation describe-stacks --stack-name Patchy-UI \
  --query 'Stacks[0].Outputs[?OutputKey==`UIUrl`].OutputValue' --output text
```

Open the URL in your browser. You'll be redirected to the Cognito hosted login page.

> **Self-signed cert warning**: If you used `setup-tls.sh` (the default), your browser will show a certificate warning. Click through -- this is expected. For production, replace with an ACM public certificate (free, auto-renewing, no warnings).

### Internal ALB Mode (No Cognito)

Set `COGNITO_ENABLED=false` in `.env` and access via SSM port forwarding:

```bash
./connect-ui.sh       # Start tunnel, then open https://localhost:8443
```

### TLS / HTTPS

| Approach | When to use |
|----------|-------------|
| `./setup-tls.sh` | Quick start / pilot. Auto-run by `deploy.sh`. Browser shows cert warning. |
| ACM public certificate | Production. Set ARN in `.env`. Free, auto-renewing, no warnings. |
| No certificate + `COGNITO_ENABLED=false` | Internal ALB only. HTTP on port 80. |

### Local Development

```bash
cd ui/api && uvicorn server:app --host 0.0.0.0 --port 8000   # Backend on port 8000
cd ui/frontend && npm run dev                                   # Frontend on port 5173
```

---

## Operator Command Reference

Prompts the system is optimized to handle. Using these patterns gives the most reliable results.

### Workflows

**Critical vulnerability response:**

| Step | Prompt | What happens |
|------|--------|-------------|
| 1 | `Show CRITICAL vulnerabilities in prod` | CVEs affecting production, CVSS scores, instance count |
| 2 | `How widespread is CVE-2026-33811?` | Fleet impact — environments, SLA deadlines |
| 3 | `Patch CVE-2026-33811 in prod` | Scope → SLA decision → execute |
| 4 | `Check status` | Automation progress per account |
| 5 | `Verify health` | SSM connectivity + CloudWatch alarms |

**Staged rollout:** `Patch CVE-X in dev` → `Check status` → `Patch CVE-X in staging` → `Check status` → `Patch CVE-X in prod`

**Routine compliance:** `Preview patches in prod` → `Check status` → `Patch all prod` → `Verify health`

### Command Patterns

| Category | Prompt | Scope |
|----------|--------|-------|
| **Discovery** | `Show instances in dev` | All managed dev instances |
| | `Show vulnerabilities in staging` | Active CVEs in staging |
| | `What's the patch compliance for prod?` | Missing patch counts |
| **Preview** | `Preview patches in prod` | Dry-run, no changes |
| | `Dry-run on i-xxx, i-yyy` | Specific instances only |
| **Patch** | `Patch CVE-2026-6772 in dev` | Severity-scoped |
| | `Patch all prod` | All severities |
| | `Patch HIGH severity in staging` | HIGH+ only |
| | `Patch instance i-0ea66eea856a9028d` | Single instance |
| **Rollback** | `Rollback i-0ea66eea856a9028d` | Single instance |
| | `Rollback dev patches` | Environment-wide |
| **Status** | `Check status` | Last automation |
| | `Check status of execution f5da8b53-...` | Specific execution |
| | `Verify health` | SSM + CloudWatch post-patch |
| **Compliance** | `Show SLA breaches this week` | Violations only |
| | `Show compliance reports` | Recent reports from S3 |
| **Windows** | `Show maintenance windows for prod` | Configured schedules |
| **Emergency** | `Emergency stop` | Cancel active automation |

### Tips

- Be specific about environment — "in dev", "in prod", "in staging"
- Include account IDs when needed — "in account 111122223333"
- Use CVE IDs for targeted patching — "Patch CVE-X in dev"
- "Preview" = read-only, "Patch" = execute
- "Check status" after any async operation
