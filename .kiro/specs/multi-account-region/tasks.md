# Multi-Account/Region — Implementation Tasks

## Prerequisites (setup steps, not built by Patchy)

Before implementing, ensure:
- [ ] Patchy is deployed in the Organizations delegated admin account
- [ ] Inspector delegated admin configured (hub account)
- [ ] `AWS-SystemsManager-AutomationExecutionRole` exists in each spoke account ([AWS docs](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-automation-multiple-accounts-and-regions.html))
- [ ] Resource Data Sync configured in spoke accounts → hub S3 compliance bucket
- [ ] SSM managed instances in spoke accounts (SSM Agent + `AmazonSSMManagedInstanceCore`)

---

## Phase 1: Foundation (1.5 days)

### Task 1.1: Account Helper Module
- [ ] Create `agent/helper/account_helper.py`
- [ ] `get_accounts(environment, region)` — reads from SSM Parameter Store `/patchy/accounts/`
- [ ] `get_client(service, account_id, region)` — STS AssumeRole into `AWS-SystemsManager-AutomationExecutionRole` with session caching (refresh 5min before expiry)
- [ ] `get_target_locations(accounts, max_concurrency, max_errors)` — builds SSM Automation TargetLocations param
- [ ] `_get_target_locations_for_scope(account_ids, regions)` — resolves scope to TargetLocations or None (single-account)

### Task 1.2: Hub IAM Updates
- [ ] `infra/lib/ui-stack.ts` — add to ECS task role:
  - `ssm:StartAutomationExecution`, `ssm:GetAutomationExecution`, `ssm:DescribeAutomationExecutions`
  - `ssm:GetParameter`, `ssm:GetParametersByPath` (for account registry)
  - `sts:AssumeRole` scoped to `arn:aws:iam::*:role/AWS-SystemsManager-AutomationExecutionRole`
- [ ] `infra/lib/core-stack.ts` — S3 bucket policy allowing spoke account writes (Resource Data Sync)
- [ ] Deploy hub stack updates and verify

### Task 1.3: Supervisor Account Awareness
- [ ] New tool `list_registered_accounts(environment)` in `agent/helper/tools.py`
- [ ] Register tool with supervisor in `agent/supervisor.py`
- [ ] Add multi-account resolution instructions to supervisor system prompt (see design.md)
- [ ] Test: register a spoke account in Parameter Store, verify supervisor resolves "patch prod"

---

## Phase 2: Core Multi-Account (2 days)

### Task 2.1: Cross-Account Patching — patch_dry_run()
- [ ] Add `account_ids` and `regions` optional parameters
- [ ] If multi-account scope: `start_automation_execution()` with TargetLocations
- [ ] If single-account (no registry or local only): keep existing `send_command()` path unchanged
- [ ] New `_poll_automation_execution()` helper — polls `get_automation_execution()` until complete
- [ ] New `_parse_automation_results()` — normalises Automation response to same shape as send_command results
- [ ] Verify `BaselineOverride` parameter works with TargetLocations (pass as Automation parameter)
- [ ] Test: dry-run against instances in spoke account

### Task 2.2: Cross-Account Patching — execute_patch_operation()
- [ ] Same `send_command` → `start_automation_execution` migration
- [ ] Immediate mode: Automation execution with TargetLocations
- [ ] Scheduled mode: register task in spoke account's maintenance window via `get_client('ssm', account_id, region)`
- [ ] Dry-run gate works cross-account (queries spoke patch states via assume-role before executing)
- [ ] Test: emergency patch on spoke account instances

### Task 2.3: Cross-Account Patching — rollback_patches()
- [ ] Migrate to Automation execution for cross-account rollback
- [ ] `verify_rollback()` — query spoke account patch states via `get_client()`
- [ ] Test: rollback on spoke account, verify state comparison works

### Task 2.4: Cross-Account Discovery
- [ ] `get_vulnerability_findings()` — parse `account_id` from Inspector resource ARNs, include in grouped response
- [ ] `assess_fleet_impact()` — use `get_client('ec2', account_id, region)` for cross-account describe_instances, add account grouping to response
- [ ] `get_patch_compliance()` — query aggregated Resource Data Sync data in hub S3 by account_id prefix
- [ ] Test: query Inspector findings spanning two accounts

### Task 2.5: Cross-Account Health & Maintenance Windows
- [ ] `check_instance_health()` — iterate registered accounts, use `get_client('ssm', ...)` per account, merge results with account_id context
- [ ] `check_cloudwatch_alarms()` — same pattern with `get_client('cloudwatch', ...)`
- [ ] `get_maintenance_windows()` — query per spoke account via `get_client()`
- [ ] `create_maintenance_window()` — create in spoke account via `get_client()`
- [ ] Test: health check and maintenance window query across two accounts

### Task 2.6: Compliance Schema Update
- [ ] Add `account_id` field to compliance report JSON schema in `generate_compliance_report()`
- [ ] Update S3 key prefix: `{account_id}/{region}/{date}/{cve_id}-report.json`
- [ ] `query_compliance_reports()` — support `account_id` filter parameter
- [ ] Test: generate and query reports across accounts

---

## Dependencies & Risks

| Risk | Mitigation |
|------|------------|
| `AWS-SystemsManager-AutomationExecutionRole` not present in spoke | Document as prerequisite; add validation in `list_registered_accounts()` |
| BaselineOverride parameter format with TargetLocations | Test early in Task 2.1; fallback to per-account send_command if needed |
| Resource Data Sync lag (compliance data not real-time) | Document expected delay; add `last_sync` timestamp to compliance queries |
| STS session expiry during long Automation executions | Refresh at 5min before expiry; catch `ExpiredTokenException` and retry |
| Backward compatibility — single-account must keep working | All multi-account paths gated on `get_accounts()` returning non-empty list |
| Automation execution polling timeout | Default 30min; configurable via parameter |

---

## Acceptance Criteria

- [ ] `./deploy.sh ui` succeeds with no accounts registered (single-account mode unchanged)
- [ ] Register spoke account via CLI → supervisor resolves "patch prod" to correct account
- [ ] `patch_dry_run()` with spoke account in registry → SSM Automation execution with TargetLocations visible in console
- [ ] `execute_patch_operation()` patches instances in spoke account
- [ ] Compliance report includes `account_id` field and correct S3 key prefix
- [ ] All existing integration tests pass with no accounts registered
