# Auditing and Compliance — Intelligent Patch Automation

This one's for compliance officers, security operations teams, and auditors — anyone who needs to verify what the system did, who kicked it off, and whether it stayed within policy. The question it answers: how do you prove to an auditor that this AI agent patched the right things, at the right time, with the right authorization?

---

## Contents

- [What gets recorded and where](#what-gets-recorded-and-where)
- [Operator identity chain](#operator-identity-chain)
- [Compliance reports (S3)](#compliance-reports-s3)
- [CloudTrail — what to look for](#cloudtrail--what-to-look-for)
- [Runtime logs (CloudWatch)](#runtime-logs-cloudwatch)
- [AgentCore traces (Generative AI Observability)](#agentcore-traces-generative-ai-observability)
- [Multi-account audit](#multi-account-audit)
- [Hardening for enterprise compliance](#hardening-for-enterprise-compliance)
- [Sample audit queries](#sample-audit-queries)

---

## What gets recorded and where

Every patch operation leaves audit evidence across several AWS services. No single service has the whole story — you get the full picture from the combination.

The primary artefact is the compliance report: a JSON file written to S3 after each patch operation completes. These land at `s3://patch-compliance-reports-<account-id>/<YYYY>/<MM>/<DD>/<execution-id>.json` and hold the operator identity, target scope, patches applied, SLA outcome, and the EMERGENCY/SCHEDULED decision. It's automatic — the operator doesn't have to do anything.

The report is written in two steps. At execution time the agent drops a context file at `s3://patch-compliance-reports-<account-id>/pending-reports/<execution-id>.json` (business data it knows up front — CVE, environment, severity, SLA, instance IDs). Once the Automation finishes, the UI backend reconciles that context with the execution outcome and writes the final dated report, then deletes the pending file.

| I want to know... | Where to look |
|---|---|
| Who initiated the patch | S3: `s3://patch-compliance-reports-<account-id>/<YYYY>/<MM>/<DD>/<execution-id>.json` → `operator` field. Or CloudWatch Logs: search `PATCH_EXECUTED: operator=` in log group `/aws/vendedlogs/bedrock-agentcore/<runtime-id>/application_logs`. |
| Why the agent chose EMERGENCY vs SCHEDULED | S3: `.../<execution-id>.json` → `execution.decision` (EMERGENCY or SCHEDULED), with `compliance.sla_hours` and `compliance.sla_source` showing the SLA that drove it. |
| Which instances were patched | S3: `.../<execution-id>.json` → `scope.instance_ids`. Or the SSM console: Automation executions → filter by document name `Patchy-*`. |
| What patches were applied | S3: `.../<execution-id>.json` → `compliance` section (before/after counts, CVE IDs, severity). |
| Whether SLA was met or breached | S3: `.../<execution-id>.json` → `compliance.sla_met`. Or a quick check via S3 metadata: `aws s3api head-object --key <key> --query Metadata.sla-met`. |
| Every AWS API call the agent made | CloudTrail → Event history → filter `userIdentity.arn` containing `AgentCore-` or `PatchySpokeRole`. |
| What tools the agent called and what came back | CloudWatch Logs: log group `/aws/vendedlogs/bedrock-agentcore/<runtime-id>/application_logs` → search `[TOOL:<name>]`. |
| The full conversation (messages + responses) | CloudWatch Logs: same log group (full model output). Or the API: `GET /api/session/{session_id}/messages`. |
| Execution trace (timing, call graph) | CloudWatch console → Generative AI Observability → Bedrock AgentCore → Sessions → filter by time range. |
| What ran in a spoke account | Assume `PatchySpokeRole` into the spoke → `aws ssm describe-automation-executions --filters Key=DocumentNamePrefix,Values=Patchy-`. |

---

## Operator identity chain

The operator's identity threads through the whole system, so every auditable artefact traces back to a human:

```
Browser login
    │
    ▼
Amazon Cognito (authenticates, issues JWT with email + groups)
    │
    ▼
ALB injects x-amzn-oidc-data header (signed ES256 JWT)
    │
    ▼
FastAPI middleware (_resolve_role) verifies JWT, extracts email
    → stores as request.state.cognito_email
    │
    ▼
Chat endpoint packages "operator": email into the agent payload
    │
    ▼
Agent runtime receives operator identity
    │
    ├──► CloudWatch Logs: PATCH_EXECUTED: operator=jane@example.com ...
    │
    ├──► S3 compliance report JSON: "operator": "jane@example.com"
    │
    └──► S3 object metadata: operator=jane@example.com
```

Key files:

- JWT verification: `ui/api/server.py` → `_verify_alb_jwt()`, `_resolve_role()`
- Operator injection into the payload: `ui/api/server.py` → `/api/chat` endpoint
- Operator in tool code: `agent/helper/tools/patch_tools.py` → `get_operator()` helper

What auditors should verify: the Cognito User Pool is the authoritative source. Cross-reference the `operator` field in the S3 reports with Cognito's `AdminListUsers` output to confirm the identity was valid at the time of execution.

---

## Compliance reports (S3)

Every completed patch operation produces a JSON report stored in S3 at:

```
s3://patch-compliance-reports-<account-id>/<YYYY>/<MM>/<DD>/<execution-id>.json
```

### Report schema (key fields)

```json
{
  "report_id": "execution-id",
  "timestamp": "2025-06-30T17:25:38+00:00",
  "operation_type": "patch",
  "execution": {
    "execution_id": "...",
    "status": "Success",
    "decision": "EMERGENCY",
    "duration_seconds": 142,
    "failure_message": "",
    "success_count": 5,
    "failure_count": 0
  },
  "vulnerability": {
    "cve_id": "CVE-2025-38477",
    "severity": "HIGH",
    "cvss_score": 7.8,
    "additional_cve_ids": []
  },
  "scope": {
    "environment": "dev",
    "targeting": "tag",
    "instance_ids": ["i-0abc...", "i-0def..."],
    "instance_count": 5,
    "account_ids": ["111111111111"],
    "regions": ["us-east-1"],
    "severity_filter": null,
    "team": "platform",
    "product": "api-gateway"
  },
  "compliance": {
    "sla_hours": 72,
    "sla_source": "tag",
    "sla_met": "Met",
    "frameworks": ["PCI-DSS"]
  },
  "patch_state": {
    "pre_patch": { "...": "..." },
    "post_patch": { "...": "..." }
  },
  "operator": "jane@example.com",
  "reconciled_at": "2025-06-30T17:28:01+00:00"
}
```

### S3 object metadata (queryable without reading the body)

Each report is stored with these S3 metadata headers:

| Key | Example value | Purpose |
|-----|--------------|---------|
| `operator` | `jane@example.com` | Who initiated it |
| `cve-id` | `CVE-2025-38477` | Which vulnerability |
| `severity` | `HIGH` | CVE severity |
| `environment` | `dev` | Target environment |
| `decision-type` | `EMERGENCY` | The agent's decision |
| `sla-met` | `Met` | SLA outcome |
| `team` | `platform` | Team attribution |
| `product` | `api-gateway` | Product attribution |
| `frameworks` | `PCI-DSS,SOC2` | Compliance frameworks |

You can query these via `s3:HeadObject` or S3 Inventory without downloading the full JSON body.

### Pending report contexts (`pending-reports/` prefix)

Between execution and the final report, there's an intermediate artefact. When the agent kicks off a patch, it writes a context file at:

```
s3://patch-compliance-reports-<account-id>/pending-reports/<execution-id>.json
```

Written by `agent/helper/tools/_shared.py`. It holds the business context the agent has at execution time — CVE, environment, severity, SLA hours and source, the EMERGENCY/SCHEDULED decision, target instance IDs, team, product, and frameworks. The UI backend later reconciles this with the Automation outcome to produce the final dated report, then deletes the pending file (`ui/api/server.py`).

If you find a file still sitting under `pending-reports/`, it means the Automation hasn't reached a terminal state yet (or the backend hasn't run its reconciliation pass). The SLA decision and reasoning an auditor cares about live in the final report's `execution.decision` and `compliance` block — there's no separate decision-log artefact.

List pending contexts:

```bash
aws s3 ls s3://patch-compliance-reports-<account-id>/pending-reports/
```

### Querying reports

AWS CLI (single report):

```bash
aws s3api head-object --bucket patch-compliance-reports-<account-id> \
  --key 2025/06/30/<execution-id>.json \
  --query 'Metadata'
```

Athena (fleet-wide analysis) — set up a Glue Crawler on the bucket, then:

```sql
SELECT report_id, timestamp, operator, cve_id, environment, decision_type, sla_met
FROM patch_compliance_reports
WHERE environment = 'prod' AND sla_met = 'Breached'
ORDER BY timestamp DESC
```

---

## CloudTrail — what to look for

CloudTrail captures every AWS API call the agent makes, attributed to the IAM principal (the AgentCore runtime role or PatchySpokeRole). The events worth searching for:

| Event name | What it means | What to check |
|---|---|---|
| `StartAutomationExecution` | The agent kicked off a patch/scan/rollback | `requestParameters.DocumentName`, `requestParameters.TargetLocations` |
| `AssumeRole` | AgentCore assumed `PatchySpokeRole` into a spoke account | `requestParameters.RoleArn`, `requestParameters.RoleSessionName` (contains the account ID + region) |
| `PutObject` | A compliance report was written to S3 | `requestParameters.bucketName`, `requestParameters.key` |
| `DescribeMaintenanceWindows` | The agent checked the maintenance window schedule | Confirms it consulted the schedule before deciding |
| `ListAssociations` | The agent checked patch policy coverage | Confirms it looked for existing policies |

Correlating CloudTrail to a specific patch operation:

1. Find the compliance report in S3 and note the `execution_id`.
2. Search CloudTrail for `StartAutomationExecution` with that execution ID in `responseElements`.
3. The CloudTrail event gives you the IAM principal, source IP (AgentCore Runtime), timestamp, and full request parameters.

Limitation: CloudTrail attributes actions to the AgentCore runtime role, not the human operator. The operator identity is captured in the S3 report and CloudWatch logs, not in CloudTrail. See [Hardening for enterprise compliance](#hardening-for-enterprise-compliance) for the IAM session-tags enhancement that closes this gap.

---

## Runtime logs (CloudWatch)

Agent runtime logs go to:

```
/aws/vendedlogs/bedrock-agentcore/<runtime-id>/application_logs
```

Enable with `ENABLE_RUNTIME_LOGS=true` in `.env`. Retention is 14 days (set by `deploy.sh`).

### Structured log prefixes

| Prefix | Meaning | Example |
|--------|---------|---------|
| `[TOOL:<name>]` | Tool invocation entry (parameters) | `[TOOL:get_patch_policy] instances=4 environment=prod` |
| `[TOOL:<name>] RESULT:` | Tool result summary | `[TOOL:patch_dry_run] RESULT: total_missing=12 instances=5` |
| `PATCH_EXECUTED:` | Patch operation completed | `PATCH_EXECUTED: operator=jane@example.com environment=dev instances=5 execution_id=abc-123` |
| `[API:<service>]` | Underlying AWS API call | `[API:list_associations] 111111111111/us-east-1 cache MISS — calling ssm:ListAssociations` |
| `[CROSS_ACCOUNT]` | Cross-account role assumption | `[CROSS_ACCOUNT] Assumed role in 111111111111/us-east-1` |
| `[STEERING]` | Steering handler fired | `[STEERING] PatchWorkflowSteering enabled for patch-automation-unified` |
| `[MEMORY]` | Memory read/write operation | `[MEMORY] Session manager for patch-automation-unified` |

### Key audit-relevant entries

For any patch operation, you should find these in sequence:

1. `[TOOL:get_maintenance_windows]` — the agent checked the schedule
2. `[TOOL:get_patch_policy]` — the agent checked for existing policies
3. `[TOOL:multi_account_dry_run]` or `[TOOL:patch_dry_run]` — the dry-run ran
4. `[TOOL:multi_account_execute]` or `[TOOL:execute_patch_operation]` — execution started
5. `PATCH_EXECUTED: operator=... environment=... instances=...` — execution completed
6. `[TOOL:check_instance_health]` — post-patch health verification

If any step is missing, the agent either skipped it (a safety concern) or the conversation was interrupted.

---

## AgentCore traces (Generative AI Observability)

AgentCore auto-instruments every model call, tool execution, and memory operation through OpenTelemetry. No code changes needed.

Where to find traces:

CloudWatch Console → [Generative AI Observability](https://console.aws.amazon.com/cloudwatch/home#/genai-observability) → Bedrock AgentCore → Sessions

What a trace shows:

- The full execution graph: user message → tool calls → LLM invocations → memory operations
- Timing per span (spot slow tools or model calls)
- Success/failure status per tool
- Token usage per model invocation
- Which steering handlers fired and what they returned

Correlating a trace to a compliance report:

1. Find the session in Generative AI Observability by time range or operator identity.
2. Look for the `execute_patch_operation` or `multi_account_execute` tool span.
3. The tool result carries the `execution_id`, which matches the S3 report's `report_id`.

Prerequisite: Transaction Search has to be enabled. `deploy.sh` does this automatically via `ensure_observability()`. To verify: CloudWatch Console → Settings → X-Ray traces → Transaction Search = "Enabled".

For more on debugging with traces, see [`docs/observability.md`](observability.md).

---

## Multi-account audit

When the agent patches spoke accounts, the audit evidence is spread across accounts:

| Evidence type | Where it lives | Account |
|---|---|---|
| SSM Automation execution history | SSM in the spoke account | Spoke |
| SSM Command invocations (RunPatchBaseline) | SSM in the spoke account | Spoke |
| CloudTrail (AssumeRole into spoke) | CloudTrail in the hub account | Hub |
| CloudTrail (StartAutomationExecution in spoke) | CloudTrail in the spoke account | Spoke |
| Compliance report | `s3://patch-compliance-reports-<hub-account-id>/<YYYY>/<MM>/<DD>/<execution-id>.json` | Hub |
| Agent runtime logs | CloudWatch Logs `/aws/vendedlogs/bedrock-agentcore/<runtime-id>/application_logs` | Hub |

Centralized alternative: set up [CloudWatch cross-account observability](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Unified-Cross-Account.html) to link spoke accounts to your hub. That surfaces spoke-side metrics and logs in the hub without manual role assumption.

---

## Hardening for enterprise compliance

These go beyond the default deployment to meet enterprise audit requirements. They line up with the [AWS Well-Architected Agentic AI Lens (AGENTOPS05-BP03)](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentops05-bp03.html) — "Implement structured logging and comprehensive audit trails."

### Immutable audit trail (S3 Object Lock)

Stop accidental or malicious deletion of compliance reports:

```bash
# Enable Object Lock on the compliance bucket (must be set at bucket creation).
# For an existing bucket, create a new one with Object Lock enabled and migrate.
aws s3api put-object-lock-configuration \
  --bucket patch-compliance-reports-<account-id> \
  --object-lock-configuration '{"ObjectLockEnabled":"Enabled","Rule":{"DefaultRetention":{"Mode":"COMPLIANCE","Years":7}}}'
```

In COMPLIANCE mode, even the root user can't delete reports until the retention period is up.

### KMS encryption with an audit trail

Replace S3-managed encryption with a customer-managed KMS key:

1. Create a KMS key with key rotation enabled.
2. Set `encryption: s3.BucketEncryption.KMS_MANAGED` in `infra/lib/core-stack.ts` (or pass a key ARN).
3. CloudTrail then logs every `kms:Decrypt` call against the key, so auditors can see who accessed which report.

### IAM session tags for CloudTrail attribution

Right now CloudTrail shows the AgentCore runtime role as the principal. To attribute actions to the human operator in CloudTrail itself:

```python
# In agent/helper/cross_account.py, add session tags to the assume_role calls:
resp = sts.assume_role(
    RoleArn=role_arn,
    RoleSessionName=f"patchy-{account_id}-{region}",
    Tags=[{'Key': 'operator', 'Value': operator_email}]  # <-- add this
)
```

CloudTrail then records `principalTag/operator=jane@example.com` on every event, giving you end-to-end operator attribution without leaning on log correlation.

> Note: this needs the spoke role trust policy to allow `sts:TagSession`. Add the action in `spoke-iam-stack.ts`.

### CloudTrail Lake for long-term retention

Default CloudTrail event history keeps 90 days. For a 7-year compliance retention:

1. Create a [CloudTrail Lake event data store](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/cloudtrail-lake.html).
2. Set the retention to match your compliance framework (e.g., 2555 days for 7 years).
3. Query it with SQL: `SELECT * FROM event_data_store WHERE eventName = 'StartAutomationExecution'`.

### Cross-account log replication

Replicate the compliance reports bucket to a dedicated audit account:

1. Enable [S3 Cross-Region Replication](https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication.html) to a bucket in a separate, restricted AWS account.
2. Give the audit account no delete permissions for the operations team.
3. This gets you tamper-evident evidence — even if the hub account is compromised, the audit copy holds.

### Log integrity validation

For environments that need cryptographic proof the logs weren't tampered with:

1. Enable [CloudTrail log file integrity validation](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/cloudtrail-log-file-validation-intro.html) — it produces digest files you can use to prove log entries haven't been modified.
2. Store the digest files in the same cross-account audit bucket.

---

## References

- [AWS Well-Architected Agentic AI Lens — Observability and monitoring](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentops05.html)
- [AGENTOPS05-BP03 — Implement structured logging and comprehensive audit trails](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentops05-bp03.html)
- [AGENTSEC05-BP01 — Implement comprehensive logging and decision artifact storage](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentsec05-bp01.html)
- [Monitoring and Auditing AI Workloads on AWS](https://aws-observability.github.io/observability-best-practices/ai/genai/recipes/monitoring_and_auditing_ai_workloads/)
- [Amazon Bedrock AgentCore Observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability.html)
- [Build trustworthy AI agents with Amazon Bedrock AgentCore Observability](https://aws.amazon.com/blogs/machine-learning/build-trustworthy-ai-agents-with-amazon-bedrock-agentcore-observability/)
