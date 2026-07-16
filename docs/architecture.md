# Architecture — Intelligent Patch Automation

Detailed architecture documentation for the patch management system. A single agent with 24 tools and direct tool selection.

| Looking for... | Jump to |
|----------------|---------|
| How instances are discovered | [Fleet Discovery](#fleet-discovery) |
| How CVEs are found and assessed | [Vulnerability Discovery](#vulnerability-discovery) |
| How patching decisions are made | [SLA Decision Flow](#sla-decision-flow) |
| How multi-account execution works | [Patch Execution](#severity-scoped-patching) |
| How the agent chooses tools | [Agent Decision Architecture](#agent-decision-architecture) |
| What infrastructure is deployed | [Infrastructure Stacks](#infrastructure-stacks) |
| How to monitor cost and performance | [Observability](observability.md) (full guide) / [Cost Tracking](#cost-tracking) |

---

## How It Works

### Fleet Discovery

The agent and dashboard discover instances through [AWS Systems Manager (SSM) Explorer](https://docs.aws.amazon.com/systems-manager/latest/userguide/Explorer.html) using the `GetOpsSummary` API with a [Resource Data Sync](https://docs.aws.amazon.com/systems-manager/latest/userguide/Explorer-resource-data-sync.html). This provides cross-account, cross-region fleet visibility in a single API call.

Explorer aggregates [OpsData](https://docs.aws.amazon.com/systems-manager/latest/userguide/Explorer.html#Explorer-opsdata-sources) — operational metadata from AWS services (EC2, SSM, Config, Patch Manager) — into a queryable store. The `GetOpsSummary` API queries this store.

**Data pipeline:**

```
EC2 Instances → AWS Config (discovers resources) → SSM Explorer OpsData → Resource Data Sync → GetOpsSummary
```

- `deploy.sh` auto-creates a sync named `patchy-fleet-sync` in the hub region
- The agent queries `GetOpsSummary` with `SyncName=patchy-fleet-sync` and `ResultAttributes=[AWS:EC2InstanceInformation]`
- Returns instance IDs, account IDs, regions, platform, and managed status for the entire fleet
- The agent then enriches each instance with EC2 tags (Environment, PatchAutomation scope) and SSM patch state via per-account lookups

**Prerequisites** (see [README](../README.md#prerequisites) for setup steps):

| Component | Purpose | How it gets set up |
|-----------|---------|-------------------|
| [Quick Setup: Config Recording](https://docs.aws.amazon.com/systems-manager/latest/userguide/quick-setup-config.html) | Config discovers EC2 instances — **required** for Explorer to see them | Console: Quick Setup → Config Recording → entire org |
| [Quick Setup: Host Management](https://docs.aws.amazon.com/systems-manager/latest/userguide/quick-setup-host-management.html) | SSM inventory collection, agent updates, patch scans | Console: Quick Setup → Host Management → entire org |
| [SSM Explorer](https://docs.aws.amazon.com/systems-manager/latest/userguide/Explorer-setup.html) | Activates OpsData sources | Console: Explorer → Enable Explorer |
| Resource Data Sync | Aggregates data across accounts into the hub | `deploy.sh` creates `patchy-fleet-sync` automatically |

> **First-time delay**: After AWS Config is initially activated in an account, Explorer takes up to 6 hours to populate EC2 instance data. This is a one-time delay per account.

> **Why Config?** SSM Explorer uses Config's resource discovery to populate the `AWS:EC2InstanceInformation` OpsData type. Without Config recording in an account, Explorer returns zero instances for that account even if SSM Agent is running.

### Vulnerability Discovery

The agent queries Amazon Inspector for active CVE findings across your fleet. It doesn't just list them -- it assesses fleet-wide impact across all environments to show how many instances are affected, which teams own them, and what SLA applies.

- Query by CVE ID, severity, or environment
- CVSS score correlation with Inspector findings
- Instance-level affected resource mapping
- Cross-environment exposure: "CVE-2025-38477 affects 4 instances in dev, 3 in staging, 0 in prod"

> **Using a third-party scanner?** Feed findings into Inspector via the [SBOM integration](https://docs.aws.amazon.com/inspector/latest/user/sbom-generator.html), or add a tool in `agent/helper/tools/vulnerability_tools.py` that queries your scanner's API directly. No routing or decision logic changes needed.

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

The agent can query these reports for trend analysis, SLA breach history, and executive summaries broken down by severity, environment, or team.

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

Conversation turns are persisted to AgentCore Memory via `AgentCoreMemorySessionManager`, configured as `session_manager=` on the agent. The session manager handles STM loading, per-message persistence, and session continuity internally.

Sessions survive page refreshes — the frontend stores the `session_id` and the operator's Cognito email in localStorage. On mount, the chat panel calls `GET /api/session/{session_id}/messages` which reads the same Memory store via `MemoryClient.get_last_k_turns`, parses each turn's Strands `SessionMessage` JSON blob, filters out `toolUse`/`toolResult` frames, and rehydrates the visible conversation. Sign-out and a different-user sign-in both clear the session keys, so a fresh user starts a clean chat. The chat panel and the agent always read from the same source of truth — Memory.

### Long-Term Memory (LTM)

After each conversation, AgentCore asynchronously extracts:
- **Semantic facts**: "CVE-2024-6387 required rollback on 2 prod instances", "staging instances often have stale SSM agents"
- **Session summaries**: Compressed view of what happened in previous sessions

LTM retrieval is configured via `RetrievalConfig` on the memory config, and the session manager automatically retrieves relevant records on each user message.

### Operator Isolation

The `actor_id` is scoped as `patch-automation/{operator_identity}`, ensuring memory isolation between operators. Operator A's extracted facts are invisible to Operator B.

### Memory Access

Full read/write via `AgentCoreMemorySessionManager` (STM). LTM retrieval is disabled (injects stale command IDs).

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

## Agent Decision Architecture

The agent uses a 5-layer approach to guide tool selection without relying on verbose system prompts. Intelligence is pushed into structural mechanisms so the prompt stays lean (~300 tokens) while maintaining correct behavior across all 24 tools.

### Layer 1: Tool Result `next_action` Hints

Every critical tool return includes a `next_action` field telling the model what to do next. This costs zero prompt tokens and is contextually specific to the exact state.

```python
# Example: resolve_execution_scope returns
{
    "accounts": [...],
    "total_instances": 12,
    "next_action": "Present the account plan to the operator. Then call multi_account_dry_run or multi_account_execute with these account_ids."
}
```

### Layer 2: Tool `Decision:` Docstrings

Six tools that are most commonly misrouted include a `Decision:` line in their docstring. This appears in the tool schema (~15 tokens per tool) and replaces verbose routing rules in the prompt.

```python
def execute_patch_operation(...):
    """Execute patching via SSM Automation (MAMR).

    Decision: Use when operator names specific instance IDs (i-xxx).
    For fleet-scope patching by environment/severity/CVE, use multi_account_execute instead.
    """
```

### Layer 3: Slim System Prompt

The agent's prompt contains ONLY what layers 1-2 and steering cannot encode:
- Role + scope boundary
- Counter-intuitive negative rules ("do NOT pre-scan before patching")
- Output wrapper (`<answer></answer>` tags)
- Confirmation pattern rules

The prompt is ~300 tokens — all workflow logic is delegated to tool `Decision:` docstrings and `next_action` hints.

### Layer 4: ConfirmationGoalHandler

A dedicated `SteeringHandler` that enforces the confirmation retry pattern:

1. Tool returns `status=confirmation_required` with a plan
2. LLM presents the plan to the operator
3. Operator approves ("yes", "proceed")
4. Handler ensures the LLM calls the same tool with `confirm_execute=True`

Without this, the LLM sometimes responds with text instead of retrying — inventing extra confirmation steps or telling the operator the patch is blocked. The handler fires only on text-only responses when a confirmation is pending.

### Layer 5: Pre-Deploy Eval Gate

An automated tool-selection evaluation runs before every deployment (`agent/eval/run_eval.py`). It feeds the actual system prompts + tool schemas to the model and validates correct tool selection across 20 scenarios.

```bash
./deploy.sh agent
# → run_eval_gate() fires first
# → if accuracy < 80%, deploy is blocked
# → SKIP_EVAL=true to bypass
```

See `agent/eval/README.md` for details on adding scenarios and updating the baseline.

### Steering Handlers (Safety Net)

Two steering handlers are active as a backstop for edge cases that structural layers can't prevent:

| Handler | What it catches |
|---------|----------------|
| `PatchWorkflowSteering` | Path A/B misrouting, scope gate, CVE forwarding, cross-env progression |
| `ComplianceOutputSteering` | "100% compliance" claims with zero data |

These fire just-in-time (before tool calls / after model responses) and return rich corrective guidance. They're the last line of defense — the first four layers handle >90% of routing correctly without needing steering intervention.

---

## Infrastructure Stacks

| Stack | Resources | Dependencies |
|-------|-----------|-------------|
| `Patchy-Network` | VPC (10.0.0.0/16), 3 AZs, public/private/isolated subnets, 2 NAT gateways, VPC flow logs | None |
| `Patchy-Core` | S3 bucket (versioned, encrypted, 365-day lifecycle), AgentCore IAM managed policy | None |
| `Patchy-UI` | ECS cluster, Fargate service, ALB, Cognito User Pool + groups, bastion (internal mode), CloudWatch log group | Network |
| `Patchy-SampleEnv` | 15 EC2 instances (5 per env), security group, IAM role, 3 maintenance windows, 3 ALBs, 3 patch baselines, CloudWatch alarms | Network |

---

## Observability & Cost

See [docs/observability.md](observability.md) for the full debugging and cost monitoring guide — covers tracing agent decisions, querying tool logs, verifying SSM command outcomes in spoke accounts, and understanding per-operation cost.
