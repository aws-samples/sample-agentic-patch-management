# Security & Production Hardening — Intelligent Patch Automation

The security architecture, hardening advice, and what it takes to make this production-ready.

---

## Authentication and Authorization

Cognito authentication is built in and on by default. The ALB authenticates users through the Cognito hosted UI before it forwards anything to Fargate. Two groups, `operators` and `viewers`, control what a user can do.

### Authentication Modes

| Mode | How it works | When to use |
|------|-------------|-------------|
| Cognito (default) | The ALB authenticates via the Cognito hosted UI. The JWT is parsed for email and group membership. | Production or pilot with a public ALB |
| API Key | Set `API_KEY_OPERATOR` and/or `API_KEY_VIEWER` in `.env`. The frontend sends the key in an `X-API-Key` header, compared in constant time. | Internal ALB with an SSM tunnel |
| No auth | With neither Cognito nor API keys configured, everyone defaults to `viewer` (read-only). Set `DEFAULT_ROLE=operator` in `.env` for a trusted network where every user should have full access. | Dev/demo only |

A few things worth calling out:

- The ALB-signed `x-amzn-oidc-data` token (which carries the user's identity and email) is signature-verified (ES256) before it's trusted. A malformed or invalid token returns 401 and doesn't fall through to a weaker auth method.
- The Cognito User Pool uses Managed Login v2, and the User Pool Client runs the SRP (`userSrp`) auth flow with the authorization-code grant. Both are provisioned in CDK (`infra/lib/ui-stack.ts`).
- API key comparison uses `secrets.compare_digest()` to avoid timing attacks.
- The `DEFAULT_ROLE` fallback is deliberately `viewer` (least privilege). Only move it to `operator` in a trusted environment.

For production, a few enhancements are worth making:

1. Verify the access-token signature against Cognito JWKS. The backend verifies the ALB-signed `x-amzn-oidc-data` token (identity), but it reads the `cognito:groups` claim — the one that decides operator vs viewer — from the `x-amzn-oidc-accesstoken` by base64-decoding it *without* checking its signature. The code logs a warning about this. In production, validate the access token's signature against the [Cognito JWKS endpoint](https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-tokens-verifying-a-jwt.html) (`https://cognito-idp.{region}.amazonaws.com/{userPoolId}/.well-known/jwks.json`) before trusting the group claim, so a forged access token can't escalate to `operator`.
2. Federate with your corporate IdP. Cognito supports OIDC/SAML federation with Okta, Ping, Azure AD, or [AWS IAM Identity Center](https://docs.aws.amazon.com/singlesignon/latest/userguide/what-is.html). Map your IdP groups to the Cognito groups (`operators`, `viewers`). See the [Cognito federation docs](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-identity-federation.html).
3. Add IAM session tags for CloudTrail attribution. Pass `--tags operator={email}` when assuming the execution role so audit trails point at the human, not just the service role.
4. Use a custom domain. Swap the ALB DNS name for a Route 53 alias with an ACM public certificate.

---

## IAM Permissions — Detailed Breakdown

### AgentCore Runtime Role (`PatchyAgentCorePolicy`)

| Service | Actions | Scope | Purpose |
|---------|---------|-------|---------|
| EC2 | `Describe*` | `*` | Read instance metadata, tags, status |
| SSM | `DescribeInstance*`, `ListCompliance*`, `Describe*PatchStates`, `DescribeMaintenanceWindows`, `GetMaintenanceWindow`, `ListAssociations`, `DescribeAssociation`, etc. | `*` | Read patch compliance, maintenance windows, patch policies |
| SSM | `CreateMaintenanceWindow`, `CreatePatchBaseline` | `*` (with an `aws:RequestTag/ManagedBy=IntelligentPatchAutomation` condition) | Create agent-managed resources only |
| SSM | `Update*`, `Delete*`, `Register*`, `Deregister*` (maintenance windows + baselines) | Scoped to `aws:ResourceTag/ManagedBy=IntelligentPatchAutomation` | Modify or delete only agent-created resources |
| SSM | `SendCommand` (documents) | `AWS-RunPatchBaseline`, `AWS-RunShellScript`, `Patchy-*` | Which documents can be invoked |
| SSM | `SendCommand` (instances) | `arn:aws:ec2:*:ACCOUNT:instance/*` with `ssm:resourceTag/PatchAutomation=enabled` | Only patch opted-in instances |
| SSM | `StartAutomationExecution`, `Get/Describe/StopAutomationExecution` | `*` | Cross-account Automation via TargetLocations |
| Inspector | `ListFindings`, `BatchGetFreeTrialInfo`, `DescribeOrganizationConfiguration`, `GetConfiguration` | `*` | Read vulnerability findings |
| S3 | `PutObject`, `GetObject`, `ListBucket` | Compliance bucket only | Write and read compliance reports |
| CloudWatch | `DescribeAlarms`, `GetMetricData`, `GetMetricStatistics`, `ListMetrics`, `DescribeAlarmHistory` | `*` | Read alarm state for health checks |
| CloudWatch Logs | `DescribeLogGroups`, `DescribeLogStreams`, `GetLogEvents` | `*` | Read logs for diagnostics |
| IAM | `CreateServiceLinkedRole` | SSM SLR only | One-time SSM setup |
| IAM | `PassRole` | Account roles, SSM only | Pass roles to SSM |
| Organizations | `ListAccounts`, `ListAccountsForParent`, `ListTagsForResource` | `*` | Discover spoke accounts |
| STS | `AssumeRole` | `arn:aws:iam::*:role/PatchySpokeRole` (with an `aws:PrincipalOrgID` condition) | Cross-account operations |

### Fargate Task Role (UI Container)

| Service | Actions | Scope | Purpose |
|---------|---------|-------|---------|
| EC2 | `DescribeInstances`, `DescribeTags` | `*` | Dashboard: environment cards |
| SSM | `DescribeInstanceInformation`, `DescribeInstancePatchStates` | `*` | Dashboard: patch compliance |
| Inspector | `ListFindings` | `*` | Dashboard: vulnerability table |
| S3 | `GetObject`, `ListBucket` | Compliance bucket only | Dashboard: compliance reports |
| STS | `GetCallerIdentity`, `AssumeRole` | `*` / `PatchySpokeRole` | Account ID resolution, cross-account dashboard queries |
| Organizations | `ListAccounts` | `*` | Auto-discover spoke accounts (multi-account mode only) |
| AgentCore | `InvokeAgentRuntime` | `arn:aws:bedrock-agentcore:REGION:ACCOUNT:runtime/*` | Chat: invoke the agent |
| AgentCore | `ListEvents`, `GetEvent` | `arn:aws:bedrock-agentcore:REGION:ACCOUNT:memory/*` | Chat: read conversation history for session rehydration |

### Cross-account trust model (`PatchySpokeRole`)

The spoke role (`infra/lib/spoke-iam-stack.ts`) trusts the hub account root, but an `ArnLike` condition narrows down which hub principals can actually assume it:

```typescript
new iam.ArnPrincipal(`arn:aws:iam::${props.hubAccountId}:root`).withConditions({
  ArnLike: {
    'aws:PrincipalArn': [
      `arn:aws:iam::${props.hubAccountId}:role/AgentCore-*`,
      `arn:aws:iam::${props.hubAccountId}:role/AmazonBedrockAgentCoreSDKRuntime-*`,
      `arn:aws:iam::${props.hubAccountId}:role/Patchy-UI-*`,
    ],
  },
}),
```

Only these hub role patterns can assume into spoke accounts:

- `AgentCore-*` / `AmazonBedrockAgentCoreSDKRuntime-*` — the agent runtime (patch operations, fleet queries)
- `Patchy-UI-*` — the dashboard Fargate task (read-only cross-account queries)
- The SSM service principal is also trusted, for Automation document execution

Arbitrary IAM users or other roles in the hub account can't assume `PatchySpokeRole`. That's the default — no extra hardening needed.

### StackSet topology

Cross-account resources are managed by two independent StackSets:

| StackSet | Scope | What it provisions |
|---|---|---|
| `Patchy-SpokeIam` | (Hub + spokes) × primary region | `PatchySpokeRole` IAM role |
| `Patchy-SsmDocs` | (Hub + spokes) × all `SPOKE_REGIONS` | Four SSM Automation documents (`Patchy-RunPatchBaseline`, `Patchy-RunPatchBaselineById`, `Patchy-RunRollback`, `Patchy-RunRollbackById`) |

Each deploys and destroys independently via `./deploy.sh spoke` and `./deploy.sh docs`.

---

## Native AWS Audit Capabilities

Before you build custom audit infrastructure, look at what AWS already gives you:

- CloudTrail: every SSM `StartAutomationExecution`, S3 `PutObject`, and EC2 API call is already logged with the IAM principal, source IP, timestamp, and full request parameters.
- SSM Automation History: `ssm:DescribeAutomationExecutions` returns the full execution history — status, timing, target accounts/regions, and the document name.
- S3 Object Metadata: compliance reports carry S3 object metadata (`operator`, `cve-id`, `environment`, `severity`, `decision-type`, `sla-met`, `team`, `product`, `frameworks`), queryable via S3 Inventory + Athena without reading the JSON body.
- CloudWatch Logs Insights: agent logs include structured `PATCH_EXECUTED:` entries with operator, environment, instance count, and execution ID, all searchable in real time.

The custom S3 compliance reports fill the gap CloudTrail leaves: business-level context like SLA assessment, before/after compliance delta, and framework attribution.

> Future enhancement: pass IAM session tags (e.g., `operator={email}`) on `AssumeRole` calls so CloudTrail attributes each action to the human operator, not just the service role. Right now the operator identity flows through the compliance report and CloudWatch logs, but not the CloudTrail session context.

---

## Network and Access

- Custom domain: register a domain in Route 53, point an alias record at the ALB, and use an ACM public certificate. Clean URL, no cert warnings.
- AWS Client VPN: for internal-ALB mode, deploy a Client VPN endpoint in the VPC so the wider team can reach the UI beyond individual SSM tunnels.
- VPC endpoints: add interface VPC endpoints for SSM, EC2, S3, STS, and Bedrock to cut NAT gateway traffic and data transfer cost.
- WAF: put AWS WAF in front of the public ALB for request filtering, SQL injection protection, rate limiting, and managed rule sets.

---

## Data and Compliance

- S3 bucket retention: switch `removalPolicy` to `RETAIN` and drop `autoDeleteObjects` in `core-stack.ts` to keep compliance reports after a stack teardown. The current default is `DESTROY` for a clean teardown.
- S3 Object Lock: turn on Object Lock (compliance mode) to make audit data immutable — no accidental or malicious deletion.
- KMS encryption: replace S3-managed encryption with a customer-managed KMS key. That gets you key rotation policies and CloudTrail key-usage auditing.
- Cross-account backup: replicate the compliance reports bucket to a separate audit account with S3 Cross-Region Replication.

---

## Availability and Scaling

- Multi-AZ Fargate: bump `desiredCount` to 2+ in `ui-stack.ts` for high availability. The ALB already spans multiple AZs.
- Auto-scaling: add ECS Service Auto Scaling based on CPU/memory or request count.
- Multi-region: deploy to a secondary region with Route 53 health checks for DR. The agent is stateless, so failover is straightforward.
- ALB health check tuning: tighten the thresholds for faster failover, and consider a deeper check that validates downstream connectivity.

---

## Agent Reliability

- Guardrails: run `python agent/setup_guardrail.py` to create a sample guardrail, then extend it with more denied topics, PII redaction, and custom word filters. See the [Bedrock Guardrails docs](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html).
- Token budget limits: set per-invocation token limits so a runaway conversation can't spiral.
- Dead letter queue: add an SQS DLQ for failed agent invocations.
- Idempotency: add idempotency keys so a retry doesn't patch twice.

---

## Operational Excellence

- CI/CD pipeline: add CodePipeline or GitHub Actions. Run `npx tsc --noEmit` and Python linting on PR, deploy to staging on merge, and promote with a manual approval.
- Infrastructure testing: add CDK snapshot tests and integration tests.
- CloudWatch dashboards: build one with agent invocation count, latency P50/P99, error rate, patch success rate, and SLA compliance percentage.
- CloudWatch alarms: agent error-rate spikes, unhealthy Fargate tasks, ALB 5xx rate, SLA breach count → SNS → PagerDuty/Slack.
- Cost monitoring: tag every resource with `Project=patch-automation`, set up AWS Budgets alerts, and watch Bedrock token usage in GenAI Observability.

---

## TLS

Swap the self-signed certificate for an [ACM public certificate](https://docs.aws.amazon.com/acm/latest/userguide/acm-public-certificates.html) tied to your domain — it's free, auto-renewing, and drops the browser warnings:

1. Request a certificate in ACM with DNS validation.
2. Add the CNAME record to Route 53.
3. Set the ARN in `.env` as `ACM_CERTIFICATE_ARN`.
4. Redeploy: `./deploy.sh ui`.
