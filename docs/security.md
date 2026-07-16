# Security & Production Hardening — Intelligent Patch Automation

Security architecture, hardening recommendations, and production-readiness guidance.

---

## Authentication and Authorization

Cognito authentication is built in and enabled by default. The ALB authenticates users via the Cognito hosted UI before forwarding requests to Fargate. Two groups (`operators`, `viewers`) control access.

### Authentication Modes

| Mode | How it works | When to use |
|------|-------------|-------------|
| **Cognito** (default) | ALB authenticates via Cognito hosted UI. JWT is parsed for email and group membership. | Production / pilot with public ALB |
| **API Key** | Set `API_KEY_OPERATOR` and/or `API_KEY_VIEWER` in `.env`. Frontend sends key via `X-API-Key` header. Comparison is constant-time. | Internal ALB with SSM tunnel |
| **No auth** | When neither Cognito nor API keys are configured, all users default to `viewer` (read-only). Set `DEFAULT_ROLE=operator` in `.env` for trusted networks where all users should have full access. | Dev/demo only |

**Security notes:**
- JWT parsing failure (malformed OIDC header) returns 401 — does not fall through to lower auth methods.
- API key comparison uses `secrets.compare_digest()` to prevent timing attacks.
- The `DEFAULT_ROLE` fallback is intentionally `viewer` (least privilege). Only set to `operator` in trusted environments.

For production, consider these enhancements:

1. **Federate with your corporate IdP**: Cognito supports OIDC/SAML federation with Okta, Ping, Azure AD, or [AWS IAM Identity Center](https://docs.aws.amazon.com/singlesignon/latest/userguide/what-is.html). Map your IdP groups to Cognito groups (`operators`, `viewers`). See [Cognito federation docs](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-identity-federation.html).
2. **IAM session tags for CloudTrail attribution**: Pass `--tags operator={email}` when assuming the execution role for tamper-proof audit attribution.
3. **Custom domain**: Replace the ALB DNS name with a Route 53 alias and ACM public certificate.

---

## IAM Permissions — Detailed Breakdown

### AgentCore Runtime Role (`PatchyAgentCorePolicy`)

| Service | Actions | Scope | Purpose |
|---------|---------|-------|---------|
| EC2 | `Describe*` | `*` | Read instance metadata, tags, status |
| SSM | `DescribeInstance*`, `ListCompliance*`, `Describe*PatchStates`, `DescribeMaintenanceWindows`, `GetMaintenanceWindow`, etc. | `*` | Read patch compliance, maintenance windows |
| SSM | `SendCommand`, `RegisterTaskWithMaintenanceWindow` | `*` | Run patches, register window tasks |
| Inspector | `ListFindings`, `BatchGetFreeTrialInfo` | `*` | Read vulnerability findings |
| S3 | `PutObject`, `GetObject`, `ListBucket` | Compliance bucket only | Write/read compliance reports |
| CloudWatch | `DescribeAlarms`, `GetMetricData` | `*` | Read alarm state for health checks |
| IAM | `CreateServiceLinkedRole` | SSM SLR only | One-time SSM setup |
| IAM | `PassRole` | Account roles, SSM only | Pass roles to SSM |

### Restricting SSM SendCommand Blast Radius

By default, `SendCommand` is scoped to `resources: ['*']`, meaning the agent can target any instance in the account. To restrict:

**Option 1: Tag-based condition (recommended)**
Add a condition to the IAM policy statement:

```json
{
  "Effect": "Allow",
  "Action": "ssm:SendCommand",
  "Resource": "arn:aws:ec2:*:*:instance/*",
  "Condition": {
    "StringEquals": {
      "ssm:resourceTag/ManagedBy": "IntelligentPatchAutomation"
    }
  }
}
```

Then tag all managed instances with `ManagedBy: IntelligentPatchAutomation`.

**Option 2: Resource ARN restriction**
Scope to specific instance IDs or tag-based resource groups. Less flexible but more explicit.

### Fargate Task Role (UI Container)

| Service | Actions | Scope | Purpose |
|---------|---------|-------|---------|
| EC2 | `DescribeInstances`, `DescribeTags` | `*` | Dashboard: environment cards |
| SSM | `DescribeInstanceInformation`, `DescribeInstancePatchStates` | `*` | Dashboard: patch compliance |
| Inspector | `ListFindings` | `*` | Dashboard: vulnerability table |
| S3 | `GetObject`, `ListBucket` | Compliance bucket only | Dashboard: compliance reports |
| STS | `GetCallerIdentity`, `AssumeRole` | `*` / `PatchySpokeRole` | Account ID resolution, cross-account dashboard queries |
| Organizations | `ListAccounts` | `*` | Auto-discover spoke accounts (multi-account mode only) |
| AgentCore | `InvokeAgentRuntime` | Account-scoped | Chat: invoke the agent |

**Cross-account trust model (`PatchySpokeRole`)**:

The spoke role trusts the **hub account root** (`arn:aws:iam::<hub>:root`), allowing any principal in the hub account to assume it. This enables both runtime operations (AgentCore, UI Fargate) and deployment operations (sample-env StackSet, admin troubleshooting) without requiring the deploying principal to match a specific role name pattern.

- The AgentCore runtime role assumes it for patch operations (SSM Automation with TargetLocations)
- The UI task role assumes it for read-only dashboard queries (EC2/SSM/Inspector)
- SSM service principal is also trusted for Automation document runs
- Role permissions are scoped: EC2 read-only, SSM patch operations, Inspector read-only, S3 baseline read

> **Production hardening**: For production deployments where the hub account is shared by multiple teams, restrict the trust policy to specific role ARN patterns. In `infra/lib/spoke-role-stack.ts`, replace `AccountPrincipal(hubAccountId)` with an `ArnLike`-conditioned principal:
>
> ```typescript
> new iam.ArnPrincipal(`arn:aws:iam::${props.hubAccountId}:root`).withConditions({
>   'ArnLike': { 'aws:PrincipalArn': [
>     `arn:aws:iam::${props.hubAccountId}:role/AgentCore-*`,
>     `arn:aws:iam::${props.hubAccountId}:role/Patchy-UI-*`,
>   ]},
> }),
> ```
>
> This prevents arbitrary hub roles or IAM users from assuming into spoke accounts. The tradeoff is that `./sample-env.sh deploy` will no longer be able to deploy the sample environment to spoke accounts via StackSet (the deploying principal won't match the pattern). Use CDK bootstrap trust or manual instance creation in spokes instead.

---

## Native AWS Audit Capabilities

Before building custom audit infrastructure, consider what AWS already provides:

- **CloudTrail**: Every SSM `SendCommand`, S3 `PutObject`, and EC2 API call is already logged with the IAM principal, source IP, timestamp, and full request parameters. With session tags, CloudTrail attributes actions to the human operator, not just the service role.
- **SSM Command History**: `ssm:ListCommands` returns the full execution history including the `Comment` field (populated with operator identity), status, timing, and target instances.
- **S3 Object Metadata**: Compliance reports are stored with S3 object metadata (`operator`, `cve-id`, `environment`, `severity`, `decision-type`, `sla-met`, `team`, `product`). Queryable via S3 Inventory + Athena without reading the JSON body.
- **CloudWatch Logs Insights**: Agent logs include structured `PATCH_SCHEDULED:` and `PATCH_EXECUTED:` entries searchable in real time.

The recommended approach: Cognito for authentication -> IAM session tags for attribution -> CloudTrail as the single source of truth for audit. The custom S3 compliance reports complement this with business-level context (SLA assessment, compliance delta) that CloudTrail doesn't capture.

---

## Network and Access

- **Custom domain**: Register a domain in Route 53, create an alias record pointing to the ALB, use an ACM public certificate. Clean URL, no cert warnings.
- **AWS Client VPN**: For internal-ALB mode, deploy a Client VPN endpoint in the VPC for broader team access beyond individual SSM tunnels.
- **VPC endpoints**: Add interface VPC endpoints for SSM, EC2, S3, STS, and Bedrock to eliminate NAT gateway traffic and reduce data transfer costs.
- **WAF**: Add AWS WAF to the public ALB for request filtering, SQL injection protection, rate limiting, and managed rule sets.

---

## Data and Compliance

- **S3 bucket retention**: Change `removalPolicy` to `RETAIN` and remove `autoDeleteObjects` in `core-stack.ts` to preserve compliance reports on stack teardown. Current default is `DESTROY` for clean teardown.
- **S3 Object Lock**: Enable Object Lock (compliance mode) to make audit data immutable. Prevents accidental or malicious deletion.
- **KMS encryption**: Replace S3-managed encryption with a customer-managed KMS key. Enables key rotation policies and CloudTrail key usage auditing.
- **Cross-account backup**: Replicate the compliance reports bucket to a separate audit account using S3 Cross-Region Replication.

---

## Availability and Scaling

- **Multi-AZ Fargate**: Increase `desiredCount` to 2+ in `ui-stack.ts` for high availability. The ALB already spans multiple AZs.
- **Auto-scaling**: Add ECS Service Auto Scaling based on CPU/memory or request count.
- **Multi-region**: Deploy to a secondary region with Route 53 health checks for DR. The agent is stateless, so failover is straightforward.
- **ALB health check tuning**: Tighten thresholds for faster failover. Consider a deeper health check that validates downstream connectivity.

---

## Agent Reliability

- **Guardrails**: Run `python agent/setup_guardrail.py` to create a sample guardrail. Extend with additional denied topics, PII redaction, and custom word filters. See [Bedrock Guardrails docs](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html).
- **Token budget limits**: Set per-invocation token limits to prevent runaway conversations.
- **Dead letter queue**: Add an SQS DLQ for failed agent invocations.
- **Idempotency**: Add idempotency keys to prevent duplicate patching on retry.

---

## Operational Excellence

- **CI/CD pipeline**: Add CodePipeline or GitHub Actions. Run `npx tsc --noEmit` and Python linting on PR, deploy to staging on merge, promote with manual approval.
- **Infrastructure testing**: Add CDK snapshot tests and integration tests.
- **CloudWatch dashboards**: Pre-built dashboard with agent invocation count, latency P50/P99, error rate, patch success rate, SLA compliance percentage.
- **CloudWatch alarms**: Agent error rate spikes, Fargate task unhealthy, ALB 5xx rate, SLA breach count -> SNS -> PagerDuty/Slack.
- **Cost monitoring**: Tag all resources with `Project=patch-automation`. Set up AWS Budgets alerts. Monitor Bedrock token usage via GenAI Observability.

---

## TLS

Replace the self-signed certificate with an ACM public certificate tied to your domain (free, auto-renewing, no browser warnings):
1. Request a certificate in ACM with DNS validation
2. Add the CNAME record to Route 53
3. Set the ARN in `.env` as `ACM_CERTIFICATE_ARN`
4. Redeploy: `./deploy.sh ui`

---

## Integration Patterns

The solution exposes an HTTP API with SSE streaming, making it embeddable in existing tools:

- **Slack / Microsoft Teams**: POST to `/api/chat` with the message body, consume the SSE stream, render in a channel. The agent's structured next-steps output maps to Slack interactive buttons.
- **ServiceNow / Jira**: Trigger patch operations from incident tickets. On ticket creation with a CVE tag, call the API to assess impact. On approval, execute. Write the compliance report URL back to the ticket.
- **SNS Notifications**: After long-running patch operations, publish a completion event to SNS. Subscribe Slack webhooks, email, or PagerDuty.
- **Custom dashboards**: The `/api/dashboard` endpoint returns all fleet status, vulnerability, and compliance data as JSON -- embeddable in Grafana, Datadog, or internal portals.
