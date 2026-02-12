# Backend Analysis — E2E Test Results & Findings

## Test Execution Summary

Ran 8 E2E tests via `agentcore invoke` on 2026-02-12. Infrastructure: 2 running instances (both in `test` environment), 148 active Inspector findings (50 HIGH, 98 MEDIUM), no dev/staging/prod instances.

---

## CRITICAL FINDINGS (Must Fix)

### 1. Environment Column Always Shows "All" — No Actual Environment Data
**Severity: CRITICAL | Every vulnerability response is affected**

Every vulnerability in the table shows `Environment: All` instead of the actual environment (`test`). This happens because `get_vulnerability_findings` does NOT extract the Environment tag from Inspector findings. The tool returns `affected_resources` (instance IDs) but no environment metadata.

The vuln agent then has no way to map findings to environments, so it defaults to "All". This makes the environment column useless and misleading — an operator can't tell which environments are actually affected.

**Root cause:** `get_vulnerability_findings` in tools.py doesn't extract resource tags from Inspector findings.

**Fix:** Extract environment tag from each finding's resource tags:
```python
for r in finding.get('resources', []):
    for tag in r.get('tags', {}).items():
        if tag[0] == 'Environment':
            env = tag[1]
```
Add `environment` field to each finding dict returned by the tool.

---

### 2. Patching Workflow Does NOT Generate Compliance Report
**Severity: CRITICAL | Audit trail gap**

When the agent patches instances (Test 5: "Patch test for CVE-2025-11561"), it executes the patch but does NOT call `generate_compliance_report` as Step 7 of the mandatory workflow requires. The S3 bucket has zero compliance reports after patching.

This means:
- No audit trail for patching decisions
- Compliance analyst has nothing to query
- SLA tracking is broken

**Root cause:** The patch manager prompt says "STEP 7: COMPLIANCE REPORT (always, even on failure)" but the agent stops after reporting the command_id. It treats the patching as "initiated" and waits for the user to come back, but never circles back to generate the report.

**Fix options:**
1. Add explicit instruction: "After execute_patch_operation returns, IMMEDIATELY call generate_compliance_report. Do not wait for user confirmation."
2. Or: generate the report when the user checks status and it shows Success (but this means the report is delayed)

Option 1 is better — the report should be generated at execution time with status=EXECUTING, then updated when verified.

---

### 3. "Patch Compliance Status" Routes to Wrong Agent
**Severity: HIGH | User confusion**

Query: "Show me the patch compliance status for the test environment"
→ Routed to compliance_analyst (S3 reports) instead of patch_manager (SSM patch states)

The supervisor sees "compliance" and routes to compliance_analyst, but the user wants SSM patch state data (missing patches, installed count), not S3 compliance reports.

**Root cause:** Supervisor routing prompt doesn't distinguish between "patch compliance" (SSM data → patch_manager) and "compliance reports" (S3 audit data → compliance_analyst).

**Fix:** Update supervisor ROUTING section:
```
consult_patch_manager → ... patch compliance, missing patches, installed patches, patch status ...
consult_compliance_analyst → Compliance REPORTS, SLA breach reports, audit trails, trends
```

---

### 4. "Generate Compliance Report" Routes to Wrong Agent
**Severity: HIGH | Compliance report never created**

Query: "Generate a compliance report for CVE-2025-11561 in test"
→ Routed to vulnerability_analyst (which can't generate reports) instead of patch_manager (which has the tool)

The supervisor sees "compliance report" + CVE and routes to vuln analyst. The vuln analyst does a dependency analysis and presents it as a "report" but never calls `generate_compliance_report`.

**Root cause:** Same routing ambiguity. The supervisor needs clearer rules about which agent owns report generation.

**Fix:** Add to routing: "generate compliance report → consult_patch_manager (owns generate_compliance_report tool)"

---

### 5. Next Steps Reference Non-Existent Environments
**Severity: HIGH | Misleading operator guidance**

Multiple responses suggest actions for environments that don't exist:
- "Stage CVE-2025-11561 remediation: dev → staging → prod" (only test has vulns)
- "Assess CVE-2025-11561 impact on staging" (no staging instances)
- "Assess blast radius for CVE-2025-9900 patching in prod" (no prod instances with this CVE)

An operator following these suggestions would waste time querying empty environments.

**Root cause:** The supervisor's next steps template says "After vulnerability scan → 1. Triage top CVE in prod, 2. Stage remediation plan, 3. Assess blast radius" — these are hardcoded patterns that don't adapt to actual data.

**Fix:** Add to supervisor prompt: "CRITICAL: Next steps must ONLY reference environments where vulnerabilities were actually detected. If vulns only exist in test, do not suggest staging/prod actions unless explicitly checking if those environments are also affected."

---

## MEDIUM FINDINGS

### 6. Impact Level Shows "LOW" Despite HIGH CVSS (Confusing Label)
**Severity: MEDIUM | User trust**

CVE-2025-11561 (CVSS 8.8, HIGH severity) shows "Impact Level: LOW" in the vuln table. The agent explains this correctly ("no external exposure, test environment") but the table juxtaposition of HIGH severity + LOW impact is confusing.

The vuln agent response does explain: "Despite the HIGH CVSS score, the actual business risk is mitigated by the test environment context and lack of external exposure." — this is good. But the table column header "Impact Level" is ambiguous.

**Fix:** Rename column from "Impact Level" to "Exposure" in the vuln agent prompt. Or better: use "Business Risk" which combines CVSS + exposure + environment.

---

### 7. Vulnerability Count: "50 findings" vs Unique CVE Count Not Explicit
**Severity: MEDIUM | Operator confusion**

The agent says "50 HIGH severity vulnerabilities" and "Showing 10 of 50". But some CVEs appear on multiple instances (same CVE on 2 instances = 2 findings). The actual unique CVE count is lower.

The tool returns `total_count: 50` (findings) but doesn't return a `unique_cve_count`. The agent sometimes deduplicates in its table but the header count is findings-based.

**Fix:** Add `unique_cve_count` to `get_vulnerability_findings` return value:
```python
unique_cves = len(set(f['cve_id'] for f in findings if f.get('cve_id')))
return {"findings": findings, "total_count": total_count, "unique_cve_count": unique_cves}
```

---

### 8. SLA Calculation Uses Command Time, Not Discovery Time
**Severity: MEDIUM | Audit accuracy**

In `generate_compliance_report`, `time_to_patch_hours` is calculated as:
```python
discovered_at = invocation['RequestedDateTime']  # When patch command was sent
patched_at = invocation.get('ResponseFinishedDateTime')
```

This measures patch execution duration (minutes), not time from vulnerability discovery to remediation (days/weeks). The SLA clock should start when Inspector first detected the vulnerability.

**Fix:** Accept `discovered_at` as an optional parameter from the vulnerability findings (`firstObservedAt` field from Inspector). Show both metrics in the report.

---

### 9. `execute_patch_operation` Always Uses `AWS-RunPatchBaseline`
**Severity: MEDIUM | Remediation effectiveness**

All patching uses `AWS-RunPatchBaseline`. But CVE-2025-11561 is in SSSD packages (libsss_idmap, libsss_nss_idmap) which may not be in the SSM patch baseline. The agent correctly identified the CVE is still ACTIVE after patching — this is likely why.

**Fix:** Add a `remediation_type` parameter to `execute_patch_operation`:
- `"baseline"` → AWS-RunPatchBaseline (default, OS patches)
- `"package"` → `yum update <package>` (specific application packages)

---

### 10. Compliance Analyst Says "100% Compliance Rate" When No Reports Exist
**Severity: MEDIUM | Misleading**

When there are zero compliance reports, the agent says "100% compliance rate" which is mathematically wrong (0/0 ≠ 100%). It should say "No data available" or "No reports to assess."

**Root cause:** Agent interpretation, not tool issue. The tool correctly returns `total_count: 0`.

**Fix:** Add to compliance analyst prompt: "If total_reports = 0, report 'No compliance data available' — never claim 100% compliance with zero reports."

---

## LOW FINDINGS

### 11. Next Steps Format Inconsistency
**Severity: LOW | UX polish**

Some responses use the enterprise vocabulary correctly:
- "Patch CVE-2025-11561 in test starting with i-00fdc45f6a700b886"

Others fall back to generic advice:
- "Schedule recurring vulnerability scans for dev (recommend weekly)"
- "Enable compliance monitoring for test environment if it exists"

The enterprise vocabulary (TRIAGE, PATCH, STAGED ROLLOUT, VERIFY, AUDIT, ROLLBACK, BLAST RADIUS) is used ~60% of the time.

---

### 12. `get_resource_dependencies` Called Per Instance (Performance)
**Severity: LOW | Performance**

For 2 instances, the vuln agent calls `get_resource_dependencies` twice. Each call hits AWS Config + ELBv2 APIs. For larger fleets this would add significant latency.

---

### 13. Patch Group Shows "Development" for Test Environment
**Severity: LOW | Data quality**

SSM patch compliance shows `patch_group: Development` for test environment instances. This is an infrastructure config issue, not a code issue, but it could confuse operators.

---

## Recommended Fix Priority

| # | Finding | Severity | Effort | Impact |
|---|---------|----------|--------|--------|
| 1 | Environment column shows "All" | CRITICAL | Low | Every vuln response wrong |
| 2 | Patching doesn't generate compliance report | CRITICAL | Low | No audit trail |
| 3 | "Patch compliance" routes to wrong agent | HIGH | Low | Wrong data returned |
| 4 | "Generate report" routes to wrong agent | HIGH | Low | Report never created |
| 5 | Next steps reference non-existent environments | HIGH | Low | Misleading guidance |
| 6 | Impact Level label confusing | MEDIUM | Low | User confusion |
| 7 | Finding count vs unique CVE count | MEDIUM | Low | Operator confusion |
| 8 | SLA uses command time not discovery time | MEDIUM | Medium | Audit accuracy |
| 9 | Always uses RunPatchBaseline | MEDIUM | Medium | Remediation gaps |
| 10 | "100% compliance" with zero reports | MEDIUM | Low | Misleading |
| 11 | Next steps vocabulary inconsistency | LOW | Low | UX polish |
| 12 | Per-instance dependency calls | LOW | Medium | Performance |
| 13 | Patch group naming | LOW | N/A | Infra config |

## Recommended Fix Order

**Phase 1 — Prompt fixes only (no code changes, deploy in minutes):**
- Fix #3, #4: Update supervisor routing rules
- Fix #5: Constrain next steps to detected environments
- Fix #6: Rename "Impact Level" to "Exposure"
- Fix #10: Zero-report handling in compliance analyst prompt
- Fix #2: Add "generate report immediately after patching" instruction

**Phase 2 — Tool code changes (deploy required):**
- Fix #1: Add environment extraction to `get_vulnerability_findings`
- Fix #7: Add `unique_cve_count` to tool return
- Fix #8: Accept `discovered_at` parameter in `generate_compliance_report`
- Fix #9: Add `remediation_type` parameter to `execute_patch_operation`
