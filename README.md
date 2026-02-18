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

1. **Discover** affected instances and calculate SLA requirement based on compliance frameworks (PCI-DSS, SOC2, HIPAA) and vulnerability severity
2. **Maintenance window check** — find the next available SSM maintenance window for the target environment
3. **SLA decision**: if the next window is within the SLA deadline → SCHEDULED (register with window). If the window would exceed the SLA → EMERGENCY (patch immediately)
4. **Dry-run scan** — runs `AWS-RunPatchBaseline` with `Operation: Scan` (the same mechanism patching uses for Install) to show exactly which patches are missing, their severity, and classification. The operator reviews before proceeding. This is programmatically enforced — the agent cannot execute a patch Install without a recent Scan (within 2 hours) on every target instance
5. **Capture pre-patch state** — snapshot compliance metrics (missing/installed counts per instance) for before/after comparison
6. **Execute** patching via SSM (`AWS-RunPatchBaseline` with `Operation: Install`)
7. **Verify** instance health (SSM connectivity) and application health (CloudWatch alarms)
8. **Capture post-patch state** — snapshot compliance metrics after patching
9. **Generate compliance report** with before/after delta, SLA assessment, and operator identity (saved to S3)

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

Every patch operation records who initiated it:

- **Pilot mode**: The frontend role switcher identity (`operator@session-id`) is injected into the agent payload and recorded in compliance reports, SSM command Comments, S3 object metadata, and CloudWatch logs
- **Production mode**: Swap for Cognito JWT claims — the `email` or `sub` from the authenticated user replaces the session-based identity. See [Production Hardening → Authentication](#authentication-and-authorization)

The audit trail is queryable from multiple surfaces:
- S3 compliance reports (JSON `operator` field + S3 object metadata)
- SSM command history (`Comment` field includes operator identity)
- CloudWatch Logs (`PATCH_SCHEDULED:` / `PATCH_EXECUTED:` log entries include operator)
- Web UI Decision Audit Trail table (Operator column)

### Role-Based Access Control

Two roles control what operators can do:

| Role | Dashboard | Chat | Patch Execution | Reports |
|------|-----------|------|-----------------|---------|
| **Operator** | ✅ | ✅ | ✅ | ✅ |
| **Viewer** | ✅ | ❌ | ❌ | ✅ |

In pilot mode, a dropdown in the top navigation switches roles (stored in localStorage, sent via `X-Role` header). In production mode, Cognito groups determine the role and the dropdown is replaced by the authenticated user identity.

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
│              (Intelligent routing + fallback)                │
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
```

The supervisor routes queries to the right specialist automatically. If a specialist can't handle a query, the supervisor re-routes or falls back to direct AWS API access via `use_aws`.

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

> **Enterprise integration**: Most organisations already maintain environment and compliance metadata in their CMDB (ServiceNow, Device42, Jira Assets). If your CMDB drives EC2 tagging through AWS Organizations tag policies, Service Catalog, or a CI/CD pipeline, the agent picks up your existing taxonomy automatically — no manual tag mapping needed. The `Environment` and `ComplianceFrameworks` tags are configurable in `agent/config/config.json` if your naming convention differs (e.g., `env` instead of `Environment`, or `Tier` instead of `ComplianceFrameworks`).

---

## Post-Deployment Setup

After deploying the agent (`./deploy.sh agent`), run these one-time setup scripts to enable advanced features:

### Long-Term Memory (Recommended)

Enables cross-session recall — the agent remembers patching outcomes, CVE history, and operator preferences across conversations.

```bash
cd agent && python setup_memory_strategies.py
```

This adds SEMANTIC (fact extraction) and SUMMARY (session summaries) strategies to the existing memory resource. Memories are automatically scoped per operator — Operator A's extracted facts are invisible to Operator B.

### Bedrock Guardrails (Optional)

Creates a sample guardrail with topic filtering, content safety, and sensitive data masking.

```bash
cd agent && python setup_guardrail.py
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

Defined in `agent/config/config.json`. SLA hours by compliance framework and severity:

| Framework | CRITICAL | HIGH | MEDIUM | LOW |
|-----------|----------|------|--------|-----|
| PCI-DSS | 12h | 48h | 168h | 720h |
| SOC2 | 24h | 72h | 168h | 720h |
| HIPAA | 24h | 72h | 168h | 720h |
| Default | 24h | 72h | 168h | 720h |

When multiple frameworks apply to an instance, the strictest SLA wins.

### Integrating with Your Existing Policy Sources

The `agent/config/config.json` file is a starting point, not a constraint. The `_load_config()` function in `agent/helper/tools.py` is a single integration point — swap it for any of these patterns to use your organisation's existing policy data:

| Source | Integration approach |
|--------|---------------------|
| **CMDB** (ServiceNow, Device42, Jira Assets) | Replace `_load_config()` with an API call to your CMDB. Most CMDBs already map compliance frameworks to asset groups — query "given this instance, what frameworks apply and what are the SLA thresholds?" |
| **EC2 tags** | Skip the config file entirely. Put SLA hours directly on instance tags (`SLA-CRITICAL=12`, `SLA-HIGH=48`). The agent already reads tags for framework names — extend `_calculate_sla_requirement()` to read SLA hours from tags. Your CMDB team manages policy through their existing tag governance pipeline. |
| **SSM Parameter Store** | Store the SLA config as a JSON parameter in Parameter Store. Operations teams update thresholds without redeploying the agent. CloudTrail audits who changed the policy and when. |
| **S3 with versioning** | Put config.json in an S3 bucket with versioning. The agent fetches it at startup. You get version history, rollback, and cross-account sharing. Add S3 event notifications to trigger agent restarts on config change. |
| **GRC platforms** (Archer, Drata, ServiceNow GRC) | These tools are often the actual source of truth for compliance SLA requirements. An API integration that pulls "for PCI-DSS, critical vulns must be patched within X hours" directly from the GRC platform means the agent always reflects the latest policy without manual sync. |

The agent's decision logic doesn't care where the SLA thresholds come from — it just needs a framework name, a severity, and an SLA hours value.

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

- Internal ALB (no public exposure) — accessed via SSM port forwarding
- HTTPS via ACM certificate
- API key authentication (opt-in via `API_KEY` env var)
- Role-based access control: `operator` (full access) and `viewer` (dashboard read-only, no chat/patching). Configure via `API_KEY_OPERATOR` and `API_KEY_VIEWER` env vars, or use the frontend role switcher in pilot mode
- Rate limiting (20 req/min per client IP)
- Instance ID regex validation on all SSM operations
- IAM least privilege — write access scoped to SSM commands and S3 compliance bucket
- Human approval gates between environments during phased rollout
- Dry-run gate — programmatic enforcement requires a recent Scan before any Install operation
- Per-user audit trail — operator identity recorded in S3 reports, SSM comments, and CloudWatch logs

---

## Production Hardening

The solution ships ready for pilot use. For production workloads, consider the following:

### Authentication and Authorization

The pilot uses a frontend role switcher with session-based identity. For production, replace with Cognito + ALB native authentication:

1. **Cognito User Pool**: Create a user pool with two groups (`operators`, `viewers`). Federate with your corporate IdP (Okta, Azure AD, Ping) via SAML or OIDC.
2. **ALB Authentication Action**: Configure the ALB listener to authenticate via Cognito before forwarding to Fargate. ALB handles the entire OAuth2 flow — login redirect, token validation, session cookies. No application code changes needed for the auth flow itself.
3. **Backend role resolution**: ALB injects `x-amzn-oidc-data` (JWT) and `x-amzn-oidc-identity` headers. Update `_resolve_role()` in `server.py` to decode the JWT payload (ALB already validated the signature) and extract `cognito:groups` for role and `email` for operator identity. Delete the API key and X-Role header logic.
4. **IAM session tags for CloudTrail attribution**: When invoking the agent, assume the execution role with `sts:AssumeRole` and pass `--tags operator={email}`. CloudTrail then records the human identity on every downstream API call (SSM SendCommand, S3 PutObject) — giving you tamper-proof, native audit attribution without any custom logging code.

This is approximately 2-3 hours of work. The ALB-Cognito integration is the key shortcut — the frontend never handles passwords or tokens.

### Native AWS Audit Capabilities

Before building custom audit infrastructure, consider what AWS already provides:

- **CloudTrail**: Every SSM `SendCommand`, S3 `PutObject`, and EC2 API call is already logged with the IAM principal, source IP, timestamp, and full request parameters. With session tags (see above), CloudTrail attributes actions to the human operator, not just the service role.
- **SSM Command History**: `ssm:ListCommands` and `ssm:ListCommandInvocations` return the full execution history including the `Comment` field (which this solution populates with operator identity), status, timing, and target instances.
- **S3 Object Metadata**: Compliance reports are stored with S3 object metadata (`operator`, `cve-id`, `environment`, `severity`, `decision-type`, `sla-met`, `team`, `product`). Queryable via S3 Inventory + Athena without reading the JSON body.
- **AWS Config**: Tracks configuration changes over time. If enabled, you get a timeline of patch compliance state changes per instance.
- **CloudWatch Logs Insights**: Agent logs include structured `PATCH_SCHEDULED:` and `PATCH_EXECUTED:` entries with operator identity, environment, mode, and instance count — queryable in real time.

For production, the recommended approach is: Cognito for authentication → IAM session tags for attribution → CloudTrail as the single source of truth for audit. The custom S3 compliance reports complement this with business-level context (SLA assessment, compliance delta) that CloudTrail doesn't capture.

### Integration Patterns

The solution exposes an HTTP API with SSE streaming, making it embeddable in existing tools:

- **Slack / Microsoft Teams**: POST to `/api/chat` with the message body, consume the SSE stream, and render agent responses in a channel. A lightweight Lambda or Fargate sidecar can bridge the Slack Events API to the patch automation API. The agent's structured next-steps output maps naturally to Slack interactive buttons.
- **ServiceNow / Jira**: Trigger patch operations from incident tickets. On ticket creation with a CVE tag, call the API to assess impact. On approval, call the API to execute. Write the compliance report URL back to the ticket.
- **SNS Notifications**: After long-running patch operations, publish a completion event to SNS. Subscribe Slack webhooks, email, or PagerDuty for operator notification without polling the chat.
- **Custom dashboards**: The `/api/dashboard` endpoint returns all fleet status, vulnerability, and compliance data as JSON — embeddable in Grafana, Datadog, or internal portals.

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
Who patched prod last week?
Generate a compliance report for PCI-DSS in staging
```

The agent will automatically assess fleet-wide impact when scanning vulnerabilities, run a dry-run preview before patching, capture before/after compliance snapshots, and include the compliance delta in the report. If rollback is needed, it verifies the rollback succeeded before reporting back.
