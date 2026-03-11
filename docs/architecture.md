# Architecture — Intelligent Patch Automation

Detailed architecture documentation for the multi-agent patch management system.

---

## How It Works

### Vulnerability Discovery

The agent queries Amazon Inspector for active CVE findings across your fleet. It doesn't just list them -- it assesses fleet-wide impact across all environments to show how many instances are affected, which teams own them, and what SLA applies.

- Query by CVE ID, severity, or environment
- CVSS score correlation with Inspector findings
- Instance-level affected resource mapping
- Cross-environment exposure: "CVE-2025-38477 affects 4 instances in dev, 3 in staging, 0 in prod"

> **Using a third-party scanner?** Feed findings into Inspector via the [SBOM integration](https://docs.aws.amazon.com/inspector/latest/user/sbom-generator.html), or add a tool in `agent/helper/tools.py` that queries your scanner's API directly. No routing or decision logic changes needed.

### Patch Policy Integration

The agent checks for existing SSM Patch Policy associations (created via Quick Setup or State Manager) before acting:

| Scenario | Agent behaviour |
|----------|----------------|
| Scheduled + no severity filter + Install policy exists | "Your patch policy will handle this during the next window. No action needed." |
| Scheduled + no severity filter + no Install policy | Registers a task with the maintenance window |
| Scheduled + severity filter | Registers a task with `BaselineOverride` scoped to requested severity |
| Emergency (any) | Direct `send_command`, with `BaselineOverride` if severity filter set |

If no patch policy exists, the agent warns: "No patch policy found. Consider creating one for consistent scheduled patching."

### Severity-Scoped Patching

SSM Patch Manager works on baseline approval rules, not individual CVE IDs. When you request patching for a specific CVE or severity, the agent uses a `BaselineOverride` -- an S3-hosted JSON file -- to scope the operation.

Pre-generated override files in the compliance S3 bucket:

| File | Scope |
|------|-------|
| `baseline-overrides/critical-only.json` | Critical only |
| `baseline-overrides/high-and-above.json` | Critical + Important (High) |
| `baseline-overrides/medium-and-above.json` | Critical + Important + Medium |
| `baseline-overrides/all-severities.json` | All severities |

Example: "Patch CVE-2025-1234 in prod" (HIGH severity CVE):
1. Agent identifies severity: HIGH
2. Selects `high-and-above` override
3. Dry-run scan shows all HIGH+ patches that will be installed
4. "To remediate CVE-2025-1234, I'll apply the HIGH+ severity baseline. This will install 12 patches including your target CVE. Here's the full list."
5. Operator confirms -> executes with the same override

> **Limitation**: SSM cannot install a single CVE's patch in isolation. The `BaselineOverride` approach scopes to severity level, not individual packages. For surgical per-CVE patching, a future enhancement could use the `InstallOverrideList` parameter with CVE -> package name mapping from Inspector findings.

> **OS support**: The pre-generated baseline overrides target Amazon Linux 2. For other operating systems, create equivalent override files with the appropriate `OperatingSystem` field. The override JSON schema is documented in the [AWS Systems Manager User Guide](https://docs.aws.amazon.com/systems-manager/latest/userguide/patch-manager-about-baselineoverride.html).

### Patch Impact Preview

Before any patch operation, the agent runs a dry-run scan showing the full blast radius -- not just the target CVE, but every package that will be updated:

- **Per-CVE patching**: "You asked to patch CVE-2025-1234 (HIGH). The HIGH+ severity baseline will install 12 patches total -- here's the full list with package names, severity, and CVE IDs for each."
- **Per-severity patching**: "Applying all CRITICAL patches will update 3 packages across 5 instances."
- **Full patching**: "The default baseline will install 47 patches. 5 are security-critical, 12 are important, 30 are medium."

The dry-run results include `cve_ids` for each patch, so the agent can confirm whether the target CVE is included. If the fix isn't available in the OS repos (common for kernel-level CVEs on Amazon Linux 2), the agent explains this.

### Rollback and Verification

If a patch causes issues, the agent runs `yum history undo` to reverse the last transaction, then verifies success via three checks:
- Re-scan with `AWS-RunPatchBaseline` confirms patch counts returned to pre-patch levels
- SSM agent connectivity confirmed on all target instances
- CloudWatch alarms back to OK state

The rollback is not reported as successful until all three pass.

> **OS limitation**: Rollback uses `yum history undo`, which only works on Amazon Linux 2 / RHEL / CentOS. See [Extending the Solution](extending.md) for adapting to other OS types.

### Compliance Reporting and SLA Enforcement

Every patch operation produces a structured JSON compliance report stored in S3 with date-partitioned keys (`YYYY/MM/DD/report-id.json`). Reports include:

- Vulnerability details (CVE ID, severity, CVSS score)
- Scope (environment, team, product, instance count)
- SLA assessment (framework, required hours, actual time-to-patch, met/breached)
- Execution details (decision type, command ID, success/failure counts)
- Before/after compliance delta (missing patches reduced from X to Y, compliance improved from N% to M%)
- Operator identity (who initiated the patch operation)

The compliance analyst agent can query these reports for trend analysis, SLA breach history, and executive summaries broken down by severity, environment, or team.

> **Integrating with your GRC/SIEM?** The reports use a consistent JSON schema -- feed them into Archer, Drata, Vanta, Splunk, or Datadog via S3 event notifications, Kinesis Firehose, or scheduled Athena queries.

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
| **operators** | Yes | Yes | Yes | Yes |
| **viewers** | Yes | No | No | Yes |

The ALB injects the Cognito JWT into request headers; the backend resolves group membership to determine the role.

When Cognito is disabled (`COGNITO_ENABLED=false`), RBAC works via:
- `X-API-Key` header mapped to `API_KEY_OPERATOR` or `API_KEY_VIEWER` environment variables
- `X-Role` header (when no API keys are configured -- open access, suitable for VPN-only environments)

> **Using IAM Identity Center (SSO)?** Federate Cognito with your Identity Center instance -- group membership flows through as JWT claims, no custom user management needed. See [Security](security.md).

---

## Memory Architecture

### Short-Term Memory (STM)

Conversation turns are persisted to AgentCore Memory via `AgentCoreMemorySessionManager` (the official Strands integration). Sessions survive page refreshes -- the frontend stores the `session_id` in localStorage.

### Long-Term Memory (LTM)

After each conversation, AgentCore asynchronously extracts:
- **Semantic facts**: "CVE-2024-6387 required rollback on 2 prod instances", "staging instances often have stale SSM agents"
- **Session summaries**: Compressed view of what happened in previous sessions

On the next session, the agent retrieves relevant LTM records and injects them as `<user_context>` before the first model call.

### Operator Isolation

The `actor_id` is scoped as `patch-automation/{operator_identity}`, ensuring memory isolation between operators. Operator A's extracted facts are invisible to Operator B.

### Specialist Memory Access

- **Supervisor**: Full read/write via `AgentCoreMemorySessionManager`
- **Specialists**: Read-only via `ReadOnlyMemoryHook` -- they see the supervisor's conversation context but don't write to memory (prevents conversation pollution)

### Namespace Pattern

```
/actor/{actorId}/strategy/{memoryStrategyId}/{sessionId}
```

For cross-session retrieval, the session ID is omitted, enabling prefix-match across all sessions for an operator.

---

## SLA Decision Flow

```
1. Read SLA-{SEVERITY} tag from EC2 instance
2. Fall back to global defaults if tag missing (CRITICAL=24h, HIGH=72h, MEDIUM=168h, LOW=720h)
3. Get next maintenance window time
4. Compare: hours_until_window vs sla_hours
   - window <= SLA  -->  SCHEDULED (register with maintenance window)
   - window > SLA   -->  EMERGENCY (patch immediately)
```

### Patch Policy Interaction

| Scenario | Decision |
|----------|----------|
| SCHEDULED + Install policy exists + no severity filter | Stop. Existing policy handles it. |
| SCHEDULED + Install policy exists + severity filter | Register severity-scoped task with maintenance window |
| SCHEDULED + no Install policy | Register task with maintenance window |
| EMERGENCY + any | Immediate `send_command` |

### Staged Rollout

When patching spans environments (dev -> staging -> prod):
- Pipeline time: ~5hr minimum (2hr validation per env + 30min gates)
- If SLA pressure exists, compress: dev validation -> 1hr, pre-stage downstream patches
- Each environment gets its own compliance report before moving to the next

---

## Infrastructure Stacks

| Stack | Resources | Dependencies |
|-------|-----------|-------------|
| `Patchy-Network` | VPC (10.0.0.0/16), 3 AZs, public/private/isolated subnets, 2 NAT gateways, VPC flow logs | None |
| `Patchy-Core` | S3 bucket (versioned, encrypted, 365-day lifecycle), AgentCore IAM managed policy | None |
| `Patchy-UI` | ECS cluster, Fargate service, ALB, Cognito User Pool + groups, bastion (internal mode), CloudWatch log group | Network |
| `Patchy-SampleEnv` | 15 EC2 instances (5 per env), security group, IAM role, 3 maintenance windows, 3 ALBs, 3 patch baselines, CloudWatch alarms | Network |
