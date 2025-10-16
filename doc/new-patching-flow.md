# Task List: Maintenance-Aware Patching Implementation

## Overview
Implement maintenance window integration with risk-based scheduling while preserving phased rollout approval gates for all patching operations.

**Key Principle**: Emergency patching bypasses maintenance windows but NOT approval gates.

**Current Stack**: `PatchAutomationVulnerabilityDriven`

---

## Phase 1: Add Maintenance Window Tools (1.5 hours)

### Task 1.1: Add get_maintenance_windows() tool
**File**: `agent/tools.py`

- [ ] Add import for SSM maintenance window operations
- [ ] Implement `get_maintenance_windows(environment: str = None)` function
- [ ] Query SSM `describe_maintenance_windows()` API
- [ ] For each window, get details via `get_maintenance_window()`
- [ ] For each window, get targets via `describe_maintenance_window_targets()`
- [ ] Calculate next execution time from cron schedule
- [ ] Filter by environment tag if specified
- [ ] Return: `window_id`, `name`, `schedule`, `next_execution_time`, `duration`, `enabled`, `target_count`, `targets`
- [ ] Add error handling for no windows found
- [ ] Add `@tool` decorator

**Expected output format**:
```json
{
  "windows": [
    {
      "window_id": "mw-xxxxx",
      "name": "dev-weekly-patching",
      "schedule": "cron(0 2 ? * SUN *)",
      "next_execution": "2025-10-20T02:00:00Z",
      "duration_hours": 2,
      "enabled": true,
      "target_count": 5,
      "targets": [{
        "resource_type": "INSTANCE",
        "filters": [{"key": "tag:Environment", "values": ["dev"]}]
      }],
      "environment": "dev"
    }
  ],
  "count": 1
}
```

### Task 1.2: Add generate_compliance_report() tool
**File**: `agent/tools.py`

- [ ] Implement `generate_compliance_report()` function
- [ ] Parameters: `cve_id`, `severity`, `decision`, `justification`, `affected_instances`, `execution_results`
- [ ] Format report as structured JSON with timestamp
- [ ] Include: CVE details, CVSS score, decision rationale, timeline, affected systems, results
- [ ] Attempt to save to S3 bucket `patch-compliance-reports-{account}` (if exists)
- [ ] S3 object key format: `{year}/{month}/{day}/{cve_id}-{environment}-{HHMMSSZ}.json`
  - Example: `2025/10/14/CVE-2025-12345-dev-120530Z.json`
  - Ensures unique keys (no overwrites)
  - Organized by date for easy lifecycle management
- [ ] If S3 fails, return report content in response
- [ ] Return: `report_url` (if S3) or `report_content` (if local)
- [ ] Add `@tool` decorator

**S3 Key Format Details**:
```python
# Generate object key
from datetime import datetime
timestamp = datetime.utcnow()
object_key = f"{timestamp.year}/{timestamp.month:02d}/{timestamp.day:02d}/{cve_id}-{environment}-{timestamp.strftime('%H%M%SZ')}.json"

# Example result
# 2025/10/14/CVE-2025-12345-dev-120530Z.json
```

**Expected output format**:
```json
{
  "report_id": "CVE-2025-12345-20251014-120530Z",
  "report_url": "s3://patch-compliance-reports-125140434174/2025/10/14/CVE-2025-12345-dev-120530Z.json",
  "summary": {
    "cve_id": "CVE-2025-12345",
    "severity": "HIGH",
    "decision": "SCHEDULED",
    "environment": "dev",
    "affected_instances": 5,
    "status": "scheduled"
  }
}
```

### Task 1.3: Import new tools in patch_manager.py
**File**: `agent/patch_manager.py`

- [ ] Add imports: `get_maintenance_windows`, `generate_compliance_report`
- [ ] Verify tools are available to agent

---

## Phase 2: Update Patch Manager Prompt (30 min)

### Task 2.1: Add maintenance window decision framework
**File**: `agent/patch_manager.py`
**Location**: After line 78 (after health check guidance)

- [ ] Add section header: `MAINTENANCE WINDOW INTEGRATION:`
- [ ] Add step 1: Query maintenance windows before patching
- [ ] Add step 2: Calculate acceptable delay based on CVSS
  - CVSS 9.0+: Max 24 hours
  - CVSS 7.0-8.9: Max 72 hours
  - CVSS 4.0-6.9: Max 7 days
- [ ] Add step 3: Decision logic (scheduled vs emergency)
- [ ] Add step 4: Always generate compliance report

### Task 2.2: Add emergency vs scheduled mode clarification
**File**: `agent/patch_manager.py`
**Location**: After decision framework

- [ ] Add section header: `EMERGENCY vs SCHEDULED PATCHING:`
- [ ] Define SCHEDULED MODE behavior:
  - Agent queries maintenance windows
  - Agent calculates if window is within acceptable delay
  - If acceptable: Agent recommends waiting for maintenance window
  - Agent generates compliance report with "SCHEDULED" decision
  - No immediate patching action - window runs automatically
  - User is informed when window will execute
- [ ] Define EMERGENCY MODE behavior:
  - Agent queries maintenance windows
  - Agent determines delay is unacceptable
  - Agent provides justification for bypassing window
  - Agent executes patches immediately using execute_patch_operation()
  - Still uses phased rollout (dev → staging → prod)
  - Still requires user approval between environments
  - Agent generates compliance report with "EMERGENCY" decision
- [ ] Add example flows for both modes

### Task 2.3: Add emergency justification template
**File**: `agent/patch_manager.py`

- [ ] Add section: `EMERGENCY PATCHING JUSTIFICATION:`
- [ ] Provide template for justification message
- [ ] Include: CVSS score, acceptable delay, actual window delay, exploitation status
- [ ] Example: "Emergency patching authorized (bypassing maintenance window): CVSS 9.8 requires action within 24 hours, next maintenance window: 5 days away (exceeds acceptable delay), active exploitation detected. Initiating phased rollout with approval gates."

---

## Phase 3: Infrastructure Updates (1 hour)

### Task 3.1: Add S3 bucket for compliance reports
**File**: `infra/lib/security-stack.ts`
**Location**: After ConfigBucket definition

- [ ] Create `ComplianceReportsBucket` with S3 construct
- [ ] Bucket name: `patch-compliance-reports-${account}`
- [ ] Enable versioning
- [ ] Enable S3 managed encryption
- [ ] Block all public access
- [ ] Set removal policy: RETAIN
- [ ] Add lifecycle rule: expire after 365 days
- [ ] Grant write access to AgentCore role
- [ ] Add CloudFormation output for bucket name

### Task 3.2: Add maintenance windows to demo instances
**File**: `infra/lib/vulnerability-driven-patching-stack.ts`
**Location**: After instance creation

- [ ] Create `DevMaintenanceWindow` with SSM construct
  - Name: `dev-weekly-patching`
  - Schedule: `cron(0 2 ? * SUN *)` (Sunday 2 AM UTC)
  - Duration: 2 hours
  - Cutoff: 0 hours
  - AllowUnassociatedTargets: false
- [ ] Register `DevWindowTarget` with SSM construct
  - WindowId: devWindow.ref
  - ResourceType: 'INSTANCE'
  - Targets: tag:Environment = dev
  - Name: 'dev-instances'
- [ ] Create `StagingMaintenanceWindow` with SSM construct
  - Name: `staging-weekly-patching`
  - Schedule: `cron(0 2 ? * TUE *)` (Tuesday 2 AM UTC)
  - Duration: 2 hours
  - Cutoff: 0 hours
  - AllowUnassociatedTargets: false
- [ ] Register `StagingWindowTarget` with SSM construct
  - WindowId: stagingWindow.ref
  - ResourceType: 'INSTANCE'
  - Targets: tag:Environment = staging
  - Name: 'staging-instances'
- [ ] Create `ProdMaintenanceWindow` with SSM construct
  - Name: `prod-monthly-patching`
  - Schedule: `cron(0 2 1 * ? *)` (1st of month, 2 AM UTC)
  - Duration: 4 hours
  - Cutoff: 1 hour
  - AllowUnassociatedTargets: false
- [ ] Register `ProdWindowTarget` with SSM construct
  - WindowId: prodWindow.ref
  - ResourceType: 'INSTANCE'
  - Targets: tag:Environment = prod
  - Name: 'prod-instances'
- [ ] Add CloudFormation outputs for window IDs

**Example Implementation**:
```typescript
// Create window
const devWindow = new ssm.CfnMaintenanceWindow(this, 'DevMaintenanceWindow', {
  name: 'dev-weekly-patching',
  description: 'Weekly maintenance window for dev environment',
  schedule: 'cron(0 2 ? * SUN *)',
  duration: 2,
  cutoff: 0,
  allowUnassociatedTargets: false
});

// Register targets
new ssm.CfnMaintenanceWindowTarget(this, 'DevWindowTarget', {
  windowId: devWindow.ref,
  resourceType: 'INSTANCE',
  targets: [{
    key: 'tag:Environment',
    values: ['dev']
  }],
  name: 'dev-instances',
  description: 'All instances tagged with Environment=dev'
});
```

### Task 3.3: Deploy infrastructure stacks
**Command**: `cd infra && cdk deploy --all --profile ri25-demo`

- [ ] Deploy PatchAutomationSecurity (for S3 bucket)
- [ ] Deploy PatchAutomationVulnerabilityDriven (for maintenance windows)
- [ ] Verify S3 bucket created
- [ ] Verify maintenance windows created
- [ ] Note bucket name and window IDs for testing

---

## Phase 4: Update AgentCore Permissions (15 min)

### Task 4.1: Add S3 permissions
**File**: `infra/lib/agentcore-permissions-stack.ts`
**Location**: In existing policy statements array

- [ ] Add S3 policy statement
- [ ] Actions: `s3:PutObject`, `s3:GetObject`, `s3:ListBucket`
- [ ] Resources: 
  - `arn:aws:s3:::patch-compliance-reports-${account}`
  - `arn:aws:s3:::patch-compliance-reports-${account}/*`

### Task 4.2: Add maintenance window permissions
**File**: `infra/lib/agentcore-permissions-stack.ts`
**Location**: In existing policy statements array

- [ ] Add SSM maintenance window policy statement
- [ ] Actions:
  - `ssm:DescribeMaintenanceWindows`
  - `ssm:GetMaintenanceWindow`
  - `ssm:RegisterTaskWithMaintenanceWindow`
  - `ssm:DescribeMaintenanceWindowTasks`
- [ ] Resources: `*` (maintenance windows don't support resource-level permissions)

### Task 4.3: Deploy permissions stack
**Command**: `cd infra && cdk deploy PatchAutomationAgentCorePermissions --profile ri25-demo`

- [ ] Deploy updated permissions
- [ ] Verify policy attached to AgentCore role
- [ ] Test permissions with AWS CLI (optional)

---

## Phase 5: Testing (1 hour)

### Task 5.1: Unit test get_maintenance_windows()
**Command**: `cd agent && python3 -c "..."`

- [ ] Test with no environment filter
- [ ] Test with environment filter (dev, staging, prod)
- [ ] Verify output format matches expected structure
- [ ] Verify next_execution_time is calculated correctly
- [ ] Test error handling (no windows found)

### Task 5.2: Unit test generate_compliance_report()
**Command**: `cd agent && python3 -c "..."`

- [ ] Test with SCHEDULED decision
- [ ] Test with EMERGENCY decision
- [ ] Verify report format is valid JSON
- [ ] Verify S3 upload (if bucket exists)
- [ ] Verify fallback to local return (if S3 fails)
- [ ] Check S3 console for uploaded report

### Task 5.3: Integration test - Scheduled patching flow (recommendation mode)
**Command**: `cd agent && agentcore invoke '{"prompt": "..."}'`

- [ ] Invoke with HIGH severity CVE (CVSS 7.5) in dev environment
- [ ] Verify agent queries maintenance windows
- [ ] Verify agent finds suitable window (within 72 hours)
- [ ] Verify agent schedules patch task (not immediate execution)
- [ ] Verify agent generates compliance report with "SCHEDULED" decision
- [ ] Verify user is notified of scheduled time
- [ ] Check SSM console for scheduled task

**Test prompt**: 
```
"Handle CVE-2025-12345 (CVSS 7.5) affecting Apache in dev environment"
```

**Expected behavior**:
```
Agent: "Checking maintenance windows for dev environment..."
Agent: "Found maintenance window: dev-weekly-patching (Sunday 2 AM)"
Agent: "Risk assessment: CVSS 7.5 allows 72-hour delay, window is 4 days away - acceptable"
Agent: "Scheduling patch task in maintenance window..."
Agent: "Patch task scheduled: task-xxxxx for Sunday 2025-10-20 02:00 AM"
Agent: "Compliance report generated: s3://patch-compliance-reports-.../CVE-2025-12345.json"
```

### Task 5.4: Integration test - Emergency patching flow
**Command**: `cd agent && agentcore invoke '{"prompt": "..."}'`

- [ ] Invoke with CRITICAL severity CVE (CVSS 9.8) with active exploitation
- [ ] Verify agent queries maintenance windows
- [ ] Verify agent determines delay unacceptable
- [ ] Verify agent provides emergency justification
- [ ] Verify agent starts immediate patching (dev first)
- [ ] Verify agent still waits for approval before staging
- [ ] Verify agent generates compliance report with "EMERGENCY" decision
- [ ] Complete phased rollout with approvals

**Test prompt**:
```
"Handle CVE-2025-99999 (CVSS 9.8) with active exploitation affecting production Apache servers"
```

**Expected behavior**:
```
Agent: "Checking maintenance windows..."
Agent: "Next production maintenance window: 25 days away"
Agent: "Emergency patching authorized (bypassing maintenance window):
       - CVSS 9.8 requires action within 24 hours
       - Next maintenance window: 25 days away (exceeds acceptable delay)
       - Active exploitation detected
       Initiating phased rollout with approval gates"
Agent: "Patching dev environment (5 instances)..."
Agent: "Command ID: cmd-xxxxx"
Agent: "Dev patching complete. Say 'proceed' to patch staging"
User: "proceed"
Agent: "Patching staging environment..."
[continues with phased rollout]
Agent: "Compliance report generated with EMERGENCY justification"
```

### Task 5.5: Integration test - Edge cases
- [ ] Test CVE with no maintenance window defined
- [ ] Test CVE with maintenance window too far in future
- [ ] Test CVE with maintenance window in past (should find next occurrence)
- [ ] Test scheduled mode with user saying "patch now" (should trigger emergency patching)
- [ ] Test emergency patching with rollback scenario
- [ ] Test with disabled maintenance window (should recommend emergency patching)
- [ ] Test compliance report when S3 bucket doesn't exist (should return report locally)
- [ ] Test with missing CVSS score (should prompt user or default to HIGH)
- [ ] Test with instances missing Environment tag (should handle gracefully)

---

## Phase 6: Documentation Updates (30 min)

### Task 6.1: Update README.md - Add maintenance-aware workflow
**File**: `README.md`
**Location**: After "Rollback Workflow" section

- [ ] Add section header: `### Maintenance-Aware Patching Workflow`
- [ ] Add 5-step workflow description
- [ ] Add decision criteria table (CVSS vs acceptable delay)
- [ ] Add example for scheduled patching
- [ ] Add example for emergency patching

### Task 6.2: Update README.md - Add testing examples
**File**: `README.md`
**Location**: In "Testing" section

- [ ] Add subsection: `### Testing Maintenance-Aware Patching`
- [ ] Add test command for scheduled patching
- [ ] Add test command for emergency patching
- [ ] Add command to view compliance reports in S3

### Task 6.3: Update README.md - Update project structure
**File**: `README.md`
**Location**: In "Project Structure" section

- [ ] Update tools.py line to show 12 tools (was 9)
- [ ] Add bullet for `get_maintenance_windows()`
- [ ] Add bullet for `schedule_patch_in_maintenance_window()`
- [ ] Add bullet for `generate_compliance_report()`

### Task 6.4: Update README.md - Update key features
**File**: `README.md`
**Location**: In "Key Features" section

- [ ] Update "Operational Excellence" bullet
- [ ] Add "Maintenance Window Integration" sub-bullet
- [ ] Add "Risk-Based Scheduling" sub-bullet
- [ ] Add "Compliance Reporting" sub-bullet

### Task 6.5: Update doc/updated_idea.md status
**File**: `doc/updated_idea.md`

- [ ] Add implementation status section at top
- [ ] Mark completed tasks
- [ ] Add deployment date
- [ ] Add testing results summary

---

## Phase 7: Agent Redeployment (15 min)

### Task 7.1: Redeploy agent to AgentCore
**Command**: `cd agent && agentcore launch --auto-update-on-conflict`

- [ ] Deploy updated agent code
- [ ] Verify deployment successful
- [ ] Check agent status
- [ ] Verify new tools are registered

### Task 7.2: Verify agent functionality
- [ ] Test basic agent invocation
- [ ] Verify tools are accessible
- [ ] Check CloudWatch logs for errors
- [ ] Confirm memory/context working

---

## Completion Checklist

### Code Changes
- [ ] 2 new tools added to tools.py (get_maintenance_windows, generate_compliance_report)
- [ ] Tools imported in patch_manager.py
- [ ] Patch Manager prompt updated with decision framework
- [ ] Emergency vs scheduled modes documented in prompt
- [ ] Justification template added

### Infrastructure Changes
- [ ] ComplianceReportsBucket created in SecurityStack
- [ ] 3 maintenance windows created in VulnerabilityDrivenPatchingStack
- [ ] 3 maintenance window targets registered
- [ ] S3 permissions added to AgentCorePermissionsStack
- [ ] Maintenance window permissions added to AgentCorePermissionsStack
- [ ] All stacks deployed successfully

### Testing Completed
- [ ] Both tools unit tested
- [ ] Scheduled patching flow tested end-to-end (recommendation mode)
- [ ] Emergency patching flow tested end-to-end
- [ ] Edge cases tested
- [ ] Compliance reports verified in S3
- [ ] Phased rollout with approvals verified

### Documentation Updated
- [ ] README.md updated with new workflow
- [ ] README.md updated with testing examples
- [ ] README.md updated with project structure (11 tools total)
- [ ] README.md updated with key features
- [ ] doc/updated_idea.md marked as implemented

### Deployment
- [ ] Agent redeployed to AgentCore
- [ ] Agent functionality verified
- [ ] No errors in CloudWatch logs

---

## Time Tracking

| Phase | Estimated | Actual | Notes |
|-------|-----------|--------|-------|
| Phase 1: Tools | 1.5 hours | | |
| Phase 2: Prompts | 0.5 hours | | |
| Phase 3: Infrastructure | 1 hour | | |
| Phase 4: Permissions | 0.25 hours | | |
| Phase 5: Testing | 1 hour | | |
| Phase 6: Documentation | 0.5 hours | | |
| Phase 7: Deployment | 0.25 hours | | |
| **Total** | **5 hours** | | |

---

## Success Criteria

✅ Agent queries maintenance windows before patching
✅ Agent makes CVSS-based scheduling decisions
✅ Emergency patching includes clear justification
✅ Emergency patching still requires approval between environments
✅ Scheduled patching uses existing maintenance windows
✅ Compliance reports saved to S3 with audit trail
✅ Both emergency and scheduled flows work in demo
✅ Phased rollout approval gates preserved for all patching

---

## Notes

- Emergency patching bypasses maintenance windows but NOT approval gates
- Scheduled patching executes during maintenance windows without approval
- All patching operations generate compliance reports
- CVSS score determines acceptable delay, not whether approval is needed
- Approval gates are environment-based (dev → staging → prod), not severity-based
