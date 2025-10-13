# Enhanced Vulnerability-Driven Patch Response

## Current State (Ad-Hoc Patching)
- CVE triggers immediate patch execution via SSM SendCommand
- No integration with existing maintenance windows
- No compliance reporting or audit trail
- Patches applied outside of scheduled maintenance periods
- Risk of disrupting business operations

## Problem Statement
**Current approach is operationally naive**: Real enterprises don't patch ad-hoc. They have:
- Established maintenance windows
- Change management processes
- Compliance reporting requirements
- Business continuity constraints

**Our demo should reflect production reality**, not toy examples.

---

## Enhanced Vulnerability-Driven Patch Response: Production-Grade Patching

### Core Principle
**Respect existing operational patterns while adding intelligence.**

The agent should:
1. Discover existing maintenance windows
2. Assess if emergency patching is justified
3. Either schedule in maintenance window OR execute immediately with justification
4. Generate compliance reports for audit

---

## Workflow Changes

### Current Flow (Ad-Hoc)
```
CVE Alert → Analyze → Find Instances → Patch Immediately → Health Check → Done
```

### Enhanced Flow (Maintenance-Aware)
```
CVE Alert → Analyze Severity → Find Instances → Check Maintenance Windows
  ├─ CRITICAL (CVSS 9.0+) → Emergency Patch + Justification Report
  └─ HIGH/MEDIUM → Schedule in Maintenance Window + Compliance Report
```

---

## Decision Framework

### Emergency Patching Criteria (Immediate Execution)
- CVSS Score ≥ 9.0 (Critical)
- Active exploitation detected (threat intel)
- Production systems exposed to internet
- No compensating controls available

**Agent must justify**: "Emergency patching authorized because [specific reasons]"

### Scheduled Patching (Maintenance Window)
- CVSS Score < 9.0
- No active exploitation
- Compensating controls available
- Existing maintenance window within acceptable timeframe

**Agent must document**: "Scheduled for [window] because risk is manageable"

---

## New Capabilities Required

### 1. Maintenance Window Discovery
```python
# Tool: get_maintenance_windows(environment)
# Returns: List of existing maintenance windows with schedules
# Logic: Check if suitable window exists within risk tolerance period
```

**Intelligence**: 
- CRITICAL CVE + 7-day maintenance window = Too long, patch now
- HIGH CVE + 2-day maintenance window = Acceptable, schedule there
- MEDIUM CVE + 7-day maintenance window = Acceptable, schedule there

### 2. Risk-Based Scheduling
```python
# Tool: schedule_patch_in_maintenance_window(instance_ids, window_id, cve_id)
# Returns: Scheduled task ID
# Logic: Register patch task with existing maintenance window
```

**Intelligence**:
- Agent calculates "acceptable delay" based on CVSS + exploitability
- Finds nearest maintenance window within acceptable delay
- If no window exists or too far out → create new window or emergency patch

### 3. Compliance Reporting
```python
# Tool: generate_compliance_report(cve_id, before_state, after_state)
# Returns: Formatted compliance report with audit trail
# Logic: Document decision rationale, execution timeline, verification results
```

**Report Contents**:
- CVE details and risk assessment
- Affected systems inventory
- Patching decision (emergency vs scheduled) with justification
- Execution timeline and results
- Before/after compliance state
- Rollback actions (if any)

---

## Enhanced Demo Flow

### Example 1: Critical CVE (Emergency)
```
User: "Handle CVE-2025-38527 (CVSS 9.8)"

Vulnerability Analyst:
→ "Critical vulnerability, active exploitation detected"
→ "Affects 15 production instances with internet exposure"

Patch Manager:
→ "Checking maintenance windows..."
→ "Next window: 5 days away"
→ "Risk assessment: Unacceptable delay for CVSS 9.8"
→ "Recommendation: Emergency patching with phased rollout"

Supervisor:
→ "Emergency patching authorized due to:"
→ "  - CVSS 9.8 (Critical)"
→ "  - Active exploitation confirmed"
→ "  - Production exposure"
→ "  - 5-day delay unacceptable"
→ "Executing phased rollout: dev → staging → prod"

[Patching executes]

Patch Manager:
→ "Generating compliance report..."
→ "Report saved to S3: s3://compliance-reports/CVE-2025-38527.json"
```

### Example 2: High CVE (Scheduled)
```
User: "Handle CVE-2025-12345 (CVSS 7.5)"

Vulnerability Analyst:
→ "High severity vulnerability, no active exploitation"
→ "Affects 8 staging instances"

Patch Manager:
→ "Checking maintenance windows..."
→ "Found: Staging maintenance window tomorrow 2:00 AM"
→ "Risk assessment: 24-hour delay acceptable for CVSS 7.5"
→ "Recommendation: Schedule in existing maintenance window"

Supervisor:
→ "Scheduled patching approved:"
→ "  - CVSS 7.5 (High, not Critical)"
→ "  - No active exploitation"
→ "  - Maintenance window available within 24 hours"
→ "Scheduling patch task in window: staging-mw-weekly"

Patch Manager:
→ "Patch task registered: task-abc123"
→ "Scheduled for: 2025-10-14 02:00 AM UTC"
→ "Notification sent to ops team"
→ "Compliance report generated"
```

---

## Implementation Changes

### Tools to Add (3 new tools)
1. `get_maintenance_windows(environment)` - Query existing windows
2. `schedule_patch_in_maintenance_window(instances, window_id, cve)` - Schedule task
3. `generate_compliance_report(cve, decision, results)` - Create audit report

### Tools to Modify (1 existing tool)
- `execute_patches()` - Add justification parameter for emergency patching

### Patch Manager Prompt Updates
Add decision framework:
```
MAINTENANCE WINDOW INTEGRATION:
1. Always check for existing maintenance windows first
2. Calculate acceptable delay based on CVSS score:
   - CVSS 9.0+: Max 24 hours
   - CVSS 7.0-8.9: Max 72 hours
   - CVSS 4.0-6.9: Max 7 days
3. If maintenance window exists within acceptable delay → schedule there
4. If no window or too far out → emergency patch with justification
5. Always generate compliance report documenting decision
```

### Infrastructure Changes
- S3 bucket for compliance reports
- SNS topic for maintenance window notifications
- EventBridge rule for scheduled patch execution (optional)

---

## Benefits of This Approach

### 1. Production Realism
- Respects existing operational patterns
- Integrates with change management
- Balances urgency with stability

### 2. Demonstrates Intelligence
- Risk-based decision making (not just "patch everything now")
- Context-aware scheduling
- Justification of emergency actions

### 3. Enterprise Requirements
- Compliance reporting for audit
- Change tracking and documentation
- Notification and coordination

### 4. Minimal Scope Creep
- Enhances existing vulnerability-driven workflow (not new scenario)
- Reuses existing agents and architecture
- 3 new tools + 1 modified tool = manageable implementation

---

## Implementation Estimate

**Phase 1: Maintenance Window Integration (2-3 hours)**
- Add `get_maintenance_windows()` tool
- Add `schedule_patch_in_maintenance_window()` tool
- Update Patch Manager prompt with decision framework
- Test scheduled vs emergency patching logic

**Phase 2: Compliance Reporting (1-2 hours)**
- Add `generate_compliance_report()` tool
- Create S3 bucket for reports
- Test report generation and storage

**Phase 3: Demo Refinement (1 hour)**
- Create maintenance windows in demo environment
- Test both emergency and scheduled flows
- Verify compliance reports

**Total: 4-6 hours**

---

## Success Criteria

✅ Agent checks maintenance windows before patching
✅ Agent makes risk-based scheduling decisions
✅ Emergency patching includes justification
✅ Scheduled patching uses existing maintenance windows
✅ Compliance reports generated for all patching actions
✅ Demo shows both emergency and scheduled flows

---

## Key Differentiator

**Before**: "Our agent patches systems when CVEs are found"
**After**: "Our agent intelligently balances security urgency with operational stability, respecting existing processes while adding risk-based decision making"

This is the difference between a toy demo and a production-ready solution.
