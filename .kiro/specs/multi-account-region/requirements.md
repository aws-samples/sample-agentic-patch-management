# Multi-Account/Region Patch Management — Requirements

## Problem Statement

Patchy currently operates within a single AWS account and region. Enterprise customers manage fleets spanning multiple accounts (dev, staging, prod in separate accounts) and regions. We need to extend Patchy to discover, patch, and report across an AWS Organization without rewriting the agent architecture.

## Guiding Principles

1. **Leverage AWS native capabilities** — SSM Automation TargetLocations, Inspector delegated admin, and Resource Data Sync handle the multi-account orchestration. Patchy adds intelligence on top, not plumbing underneath.
2. **Delegated admin model** — Patchy runs in the Organizations delegated admin account. Inspector and SSM inventory/compliance data are already aggregated here. No custom cross-account roles needed for read operations.
3. **Extensible, not over-engineered** — build the simplest thing that works and is production-extensible. Document more mature options (DynamoDB registry, StackSets enrollment, TargetLocations concurrency tuning) as production hardening.
4. **Backward compatible** — single-account deployments must continue to work with zero configuration change.

## Deployment Context

Patchy is deployed in the **Organizations delegated admin account**. This means:
- Inspector findings from all member accounts are automatically aggregated here
- SSM Resource Data Sync from spoke accounts pushes patch compliance to the hub's S3 bucket
- The ECS task role needs `ssm:StartAutomationExecution` and `sts:AssumeRole` (for read-only calls to spoke accounts where Organizations-level access is insufficient)

## Prerequisites (not built by Patchy — documented as setup steps)

- Spoke accounts are part of the same AWS Organization
- Inspector is enabled and delegated admin is configured (hub account)
- `AWS-SystemsManager-AutomationExecutionRole` exists in each spoke account (standard AWS role for SSM Automation multi-account execution — [AWS docs](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-automation-multiple-accounts-and-regions.html))
- Resource Data Sync configured in spoke accounts pointing to hub's S3 compliance bucket
- SSM managed instances in spoke accounts (SSM Agent installed, instance profile with `AmazonSSMManagedInstanceCore`)

## Requirements

### R1: Account Registry
- Operators register spoke accounts (account ID, regions, friendly name, environment tags)
- Stored in **SSM Parameter Store** (`/patchy/accounts/{account_id}`) — no new infrastructure, already in IAM policy
- Supervisor agent resolves natural language ("patch prod") to specific accounts/regions via registry lookup
- Registry is the extensibility point: swap Parameter Store for DynamoDB at scale (documented in production hardening)

### R2: Cross-Account Patching via SSM Automation TargetLocations
- Replace `send_command()` with `start_automation_execution()` + `TargetLocations` for multi-account operations
- Applies to: `patch_dry_run()`, `execute_patch_operation()`, `rollback_patches()`
- TargetLocations specifies accounts, regions, and `ExecutionRoleName: AWS-SystemsManager-AutomationExecutionRole`
- Single-account fast path preserved — if only one account in scope, keep existing `send_command()` for backward compat
- `MaxConcurrency` and `MaxErrors` configurable (default: 10% concurrency, stop at 1 error)
- BaselineOverride (severity-scoped patching) passed as Automation parameter — works with TargetLocations

### R3: Cross-Account Discovery
- `get_vulnerability_findings()` — no code change needed (Inspector delegated admin aggregates automatically, resource ARNs include account IDs)
- `assess_fleet_impact()` — parse account_id from Inspector resource ARNs, group by account in response
- `get_patch_compliance()` — query aggregated Resource Data Sync data in hub S3 (primary path); assume-role fallback for accounts not yet syncing

### R4: Cross-Account Health & Maintenance Windows
- `check_instance_health()` — assume-role per account using `account_helper.get_client()`
- `check_cloudwatch_alarms()` — assume-role per account (document CloudWatch cross-account observability as production hardening)
- `get_maintenance_windows()` / `create_maintenance_window()` — assume-role per account

### R5: Supervisor Account Awareness
- Supervisor resolves user intent to target accounts/regions using the account registry
- New tool: `list_registered_accounts()` available to supervisor
- Supports: "patch all prod accounts", "patch account 111111111111", "patch us-west-2"
- Fallback: no accounts registered → single-account mode (zero behavior change)
- Blast radius confirmation: supervisor explicitly states account scope before cross-account bulk operations

### R6: Compliance Schema Update
- Add `account_id` field to compliance report JSON schema
- S3 key prefix: `{account_id}/{region}/{date}/report.json`
- `query_compliance_reports()` supports account_id filter

### R7: Hub IAM Updates
- ECS task role: add `sts:AssumeRole` (scoped to `AWS-SystemsManager-AutomationExecutionRole` pattern)
- ECS task role: add `ssm:StartAutomationExecution`, `ssm:GetAutomationExecution`, `ssm:DescribeAutomationExecutions`
- ECS task role: add `ssm:GetParameter`, `ssm:GetParametersByPath` for account registry
- S3 bucket policy: allow spoke account writes (for Resource Data Sync)

## Production Hardening (document, don't build)

| Current (built) | Production path |
|-----------------|-----------------|
| SSM Parameter Store registry | DynamoDB with TTL, conditional writes, audit via CloudTrail |
| Manual spoke account registration | StackSets + Organizations auto-enrollment |
| Default MaxConcurrency/MaxErrors | Configurable per-operation via agent parameters |
| Assume-role for CW alarms | CloudWatch cross-account observability (Organizations sharing) |
| Manual Inspector setup | Config conformance pack for auto-enablement |
| No account health validation | Scheduled Lambda validates assume-role connectivity per account |
| No audit aggregation | EventBridge cross-account forwarding to central audit log |
