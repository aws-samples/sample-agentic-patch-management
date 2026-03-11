# Operations — Intelligent Patch Automation

Post-deployment setup, configuration, and day-to-day operations.

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
> If the Cognito User Pool is recreated (e.g., after a CDK change that replaces the pool resource), **all users are lost**. The Pool ID is in the CloudFormation outputs -- always verify it before creating users. To federate with your corporate IdP instead of managing users manually, see [Security -> Federation](security.md#federating-with-okta-ping-or-azure-ad).

---

## Long-Term Memory

Long-term memory is configured automatically during `./deploy.sh`. The script:
1. Upgrades memory mode from `STM_ONLY` to `STM_AND_LTM`
2. Adds SEMANTIC and SUMMARY extraction strategies
3. Validates the runtime role's IAM policy references the correct memory resource

If you need to re-run memory setup manually (e.g., after recreating the memory resource):
```bash
source venv/bin/activate
python agent/setup_memory_strategies.py
```

This adds SEMANTIC (fact extraction) and SUMMARY (session summaries) strategies to the existing memory resource. Memories are automatically scoped per operator via `actor_id` -- Operator A's extracted facts are invisible to Operator B.

> The AgentCore toolkit automatically injects `BEDROCK_AGENTCORE_MEMORY_ID` into the generated Dockerfile. No custom Dockerfile is needed.

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

Sample windows (from `--with-sample-env`): dev daily 1AM UTC, staging Tuesdays 2AM UTC, prod 1st of month 2AM UTC.

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

- **Dashboard**: KPI bar (total instances, vulnerabilities, SLA compliance rate), environment status cards, vulnerability table, compliance summary by severity/environment/team, decision explainability table with operator attribution
- **Chat**: Streaming agent responses with specialist routing indicators, session persistence via localStorage

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
