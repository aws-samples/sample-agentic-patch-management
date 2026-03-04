# Multi-Account/Region Patch Management — Requirements

## Problem Statement

Patchy currently operates within a single AWS account and region. Enterprise customers manage fleets spanning multiple accounts (dev, staging, prod in separate accounts) and regions. We need to extend Patchy to discover, patch, and report across an AWS Organization without rewriting the agent architecture.

## Guiding Principle

Leverage SSM's native multi-account/region capabilities (Automation TargetLocations, Resource Data Sync, Inspector delegated admin) to minimize custom code. The agents should be account-aware but the heavy lifting stays with AWS services.

## Requirements

### R1: Account Registry
- Operators can register spoke accounts (account ID, regions, execution role ARN, friendly name)
- Stored in DynamoDB table in the hub account
- Supervisor agent resolves natural language ("patch prod") to specific accounts/regions
- API endpoints for CRUD operations on the registry

### R2: Spoke Account IAM Role (CDK Stack)
- Deployable via StackSets or standalone CDK into each spoke account
- Grants: SSM RunCommand/Automation, EC2 Describe, CloudWatch Describe, S3 write to central compliance bucket
- Trust policy: hub account's ECS task role + Automation service role
- Minimal permissions — scoped to Patchy operations only

### R3: Cross-Account Vulnerability Discovery
- Inspector delegated admin in hub account aggregates findings from all member accounts
- `get_vulnerability_findings()` — no code change needed (Inspector aggregates automatically)
- `assess_fleet_impact()` — add account_id grouping to response, cross-account EC2 describe via assume-role

### R4: Cross-Account Patching via SSM Automation
- Replace `ssm_client.send_command()` with `ssm_client.start_automation_execution()` + `TargetLocations`
- Applies to: `execute_patch_operation()`, `patch_dry_run()`, `rollback_patches()`
- TargetLocations specifies accounts, regions, execution role, concurrency limits
- Dry-run gate and severity-scoped BaselineOverride must work cross-account

### R5: Cross-Account Compliance Data
- Resource Data Sync aggregates patch compliance from spoke accounts to central S3
- `get_patch_compliance()` queries aggregated data (or assumes role per account as fallback)
- `capture_patch_state()` works cross-account via Automation
- Compliance reports include account_id in schema and S3 key prefix

### R6: Cross-Account Health Checks
- `check_instance_health()` — assume role per account to query SSM instance information
- `check_cloudwatch_alarms()` — assume role per account OR use CloudWatch cross-account observability
- Health check results tagged with account_id

### R7: Cross-Account Maintenance Windows
- `get_maintenance_windows()` — query per spoke account via assume-role
- `create_maintenance_window()` — create in spoke account via assume-role
- Aggregate results with account context for the agent

### R8: Supervisor Account Awareness
- Supervisor resolves user intent to target accounts/regions using the account registry
- Enriches specialist agent context with account scope
- Supports: "patch all prod accounts", "patch account 111111111111", "patch us-west-2"

### R9: UI Account/Region Support
- Account selector dropdown (populated from registry)
- Region filter
- Dashboard cards show per-account breakdown
- Compliance reports filterable by account
- Chat context includes selected account scope

### R10: CDK Hub Stack Updates
- DynamoDB table for account registry
- IAM policy additions for STS AssumeRole to spoke accounts
- SSM Automation service-linked role configuration
- Resource Data Sync destination bucket policy
