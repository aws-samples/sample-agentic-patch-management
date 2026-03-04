# Multi-Account/Region — Implementation Tasks

## Phase 1: Foundation (2 days)

### Task 1.1: Account Registry DynamoDB Table
- [ ] Add DynamoDB table `patchy-account-registry` to `infra/lib/core-stack.ts`
- [ ] Schema: `account_id` (PK), `regions`, `role_arn`, `friendly_name`, `environments`, `enabled`
- [ ] Grant ECS task role read/write access to the table
- [ ] Deploy and verify table creation

### Task 1.2: Account Helper Module
- [ ] Create `agent/helper/account_helper.py`
- [ ] Implement `get_accounts(environment, region)` — query DynamoDB registry
- [ ] Implement `get_client(service, account_id, region)` — STS assume-role with session caching
- [ ] Implement `group_instances_by_account(instance_ids)` — parse account from ARN or registry lookup
- [ ] Implement `get_target_locations(instance_groups)` — build SSM Automation TargetLocations param
- [ ] Unit tests for session caching and account resolution

### Task 1.3: Account Registry API Endpoints
- [ ] Create `ui/api/routes/accounts.py`
- [ ] `GET /api/accounts` — list all registered accounts
- [ ] `POST /api/accounts` — register spoke account (validate role ARN is assumable)
- [ ] `PUT /api/accounts/{account_id}` — update config
- [ ] `DELETE /api/accounts/{account_id}` — deregister
- [ ] Wire routes into FastAPI app

### Task 1.4: Spoke CDK Stack
- [ ] Create `infra/lib/spoke-stack.ts` — IAM role `PatchySpokeRole` with trust to hub account
- [ ] Permissions: SSM, EC2 describe, CloudWatch describe, S3 write to central bucket
- [ ] Create `infra/bin/spoke-app.ts` — separate CDK app entry point
- [ ] Add `spoke` target to `deploy.sh`
- [ ] Test: deploy to a second account, verify `sts:AssumeRole` from hub succeeds

### Task 1.5: Hub IAM Updates
- [ ] Add `sts:AssumeRole` permission to ECS task role in `ui-stack.ts` (scoped to `PatchySpokeRole`)
- [ ] Add S3 bucket policy in `core-stack.ts` allowing spoke account writes
- [ ] Deploy hub stack updates

---

## Phase 2: Core Multi-Account (3 days)

### Task 2.1: Cross-Account Patching — patch_dry_run()
- [ ] Add `account_id` optional parameter
- [ ] If multi-account: use `start_automation_execution()` with TargetLocations
- [ ] If single-account (no registry or local only): keep existing `send_command()` path
- [ ] Parse automation execution results (different response shape than send_command)
- [ ] Verify BaselineOverride works cross-account (S3 bucket policy must allow spoke reads)
- [ ] Test: dry-run against instances in spoke account

### Task 2.2: Cross-Account Patching — execute_patch_operation()
- [ ] Same send_command → start_automation_execution migration
- [ ] Scheduled mode: register task in spoke account's maintenance window via assume-role
- [ ] Immediate mode: automation execution with TargetLocations
- [ ] Dry-run gate must work cross-account (query spoke's patch states via assume-role)
- [ ] Test: emergency patch on spoke account instances

### Task 2.3: Cross-Account Patching — rollback_patches()
- [ ] Migrate to automation execution for cross-account rollback
- [ ] `verify_rollback()` — query spoke account patch states via assume-role
- [ ] Test: rollback on spoke account, verify state comparison works

### Task 2.4: Cross-Account Discovery
- [ ] Inspector delegated admin setup (manual/documented step, not CDK)
- [ ] `get_vulnerability_findings()` — add account_id parsing from resource ARNs in response
- [ ] `assess_fleet_impact()` — cross-account EC2 describe via assume-role, add account grouping
- [ ] `get_patch_compliance()` — accept optional account_id, use assumed-role client
- [ ] Test: query Inspector findings spanning two accounts

### Task 2.5: Cross-Account Health & Maintenance Windows
- [ ] `check_instance_health()` — group by account, assume-role per account
- [ ] `check_cloudwatch_alarms()` — group by account, assume-role per account
- [ ] `get_maintenance_windows()` — query per spoke account via assume-role
- [ ] `create_maintenance_window()` — create in spoke account via assume-role
- [ ] Merge results with account_id context
- [ ] Test: health check and MW query across two accounts

### Task 2.6: Supervisor Account Awareness
- [ ] Add `list_registered_accounts()` tool to supervisor's tool list
- [ ] Update supervisor system prompt with account resolution instructions
- [ ] Supervisor enriches specialist queries with `account_ids` and `regions`
- [ ] Fallback: no accounts registered → single-account mode (zero behavior change)
- [ ] Test: "patch prod in all accounts" resolves correctly

### Task 2.7: Compliance Report Schema Update
- [ ] Add `account_id` field to compliance report JSON schema
- [ ] Update S3 key prefix: `{account_id}/{region}/{date}/report.json`
- [ ] `generate_compliance_report()` — include account_id in report
- [ ] `query_compliance_reports()` — support account_id filter
- [ ] Test: generate and query reports across accounts

---

## Phase 3: UI + Polish (2 days)

### Task 3.1: Account Selector Component
- [ ] Create `ui/frontend/src/components/AccountSelector.tsx`
- [ ] Fetch accounts from `/api/accounts`
- [ ] Dropdown with "All Accounts" default + individual account options
- [ ] Region sub-filter based on selected account's regions
- [ ] Store selection in app state (React context or URL params)

### Task 3.2: Dashboard Multi-Account View
- [ ] Dashboard cards show per-account breakdown when "All Accounts" selected
- [ ] Single-account view when specific account selected
- [ ] Compliance chart aggregates or filters by account
- [ ] Instance count, finding count per account

### Task 3.3: Compliance Page Account Support
- [ ] Add Account column to compliance reports table
- [ ] Filter by account from selector
- [ ] Report detail view shows account context

### Task 3.4: Chat Account Context
- [ ] Pass selected account scope in chat API payload
- [ ] Display account context in chat messages
- [ ] Agent responses include account references

### Task 3.5: Account Management UI
- [ ] Settings page or modal for managing spoke accounts
- [ ] Register new account (account ID, role ARN, regions, environments)
- [ ] Edit/delete existing accounts
- [ ] Connection test button (verifies assume-role works)

### Task 3.6: Integration Testing
- [ ] Update `tests/integration_test.py` with `--accounts` flag
- [ ] Multi-account test phases: cross-account discovery, patching, compliance
- [ ] Backward compat: all existing tests pass with no accounts registered
- [ ] E2E: register spoke → discover vulns → patch → verify → compliance report

---

## Dependencies & Risks

| Risk | Mitigation |
|------|------------|
| SSM Automation TargetLocations requires specific IAM setup | Spoke stack handles this; document manual steps for Organizations |
| Inspector delegated admin is a manual Organizations step | Document in README; can't automate via CDK |
| BaselineOverride S3 URLs must be accessible cross-account | Bucket policy in core-stack allows spoke reads |
| Session caching edge cases (expiry during long operations) | Refresh at 50min mark; catch ExpiredTokenException and retry |
| Backward compatibility — single-account must keep working | All multi-account paths gated on registry having entries |
