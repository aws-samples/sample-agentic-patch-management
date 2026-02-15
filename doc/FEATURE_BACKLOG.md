# Feature Backlog

Prioritized list of features to build next. Each item is labeled as NEW (doesn't exist), ENHANCEMENT (improves existing capability), or REPLACEMENT (replaces existing code).

Based on full codebase analysis of all 15 tools, 4 specialists, and the 7-step patching workflow.

---

## Priority 1: Patch Dry-Run / What-If Analysis ✅ BUILT
**Type:** NEW

**Why:** Nothing in the codebase previews what will change before patching. The workflow jumps straight from DECIDE (step 3) to EXECUTE (step 4). Operators approve blind. This is the single highest-value gap.

**What exists today:** Nothing. `execute_patch_operation` calls `AWS-RunPatchBaseline` with `Operation: Install` immediately.

**What to build:**
- New tool: `patch_dry_run(instance_ids: list[str]) -> dict`
- SSM command: `yum check-update --security` on target instances
- Parse output → package name, current version, new version, reboot required
- Return structured table data

**Integration:** Insert between step 3 (DECIDE) and step 4 (EXECUTE) in `patch_manager.py`. Agent presents dry-run results and asks for confirmation before proceeding.

**Effort:** Low (1-2 hours). Single SSM RunShellScript + output parsing.

---

## Priority 2: Post-Patch Compliance Delta ✅ BUILT
**Type:** ENHANCEMENT of `generate_compliance_report` in `tools.py`

**Why:** The compliance report (step 7) records what happened — success/failure, SLA met/breached, command status. But it never captures the *before* state, so there's no delta. "Patching succeeded" is less useful than "compliance improved from 60% → 100%, 12 CVEs resolved."

**What exists today:** `generate_compliance_report()` calls `get_command_status()` for execution results, gets instance tags for team/product, calculates SLA timing, saves JSON to S3. No pre-patch snapshot.

**What to change:**
- Before step 4 (EXECUTE): call `ssm:DescribeInstancePatchStates` to capture missing_count, installed_count per instance
- Store as `pre_patch_state` in the workflow context
- After step 6 (HEALTH CHECK): call same API for `post_patch_state`
- Add `delta` section to compliance report JSON: before/after counts, CVEs resolved, compliance % change
- Modify `generate_compliance_report()` to accept optional `pre_patch_state` and `post_patch_state` params

**Effort:** Low-medium (2-3 hours). Uses existing SSM APIs, just needs snapshot capture at two points.

---

## Priority 3: Rollback Verification ✅ BUILT
**Type:** ENHANCEMENT of `rollback_patches` in `tools.py`

**Why:** `rollback_patches()` runs `yum history undo` via SSM and returns the command_id. It never verifies the rollback actually worked. The operator has to manually check.

**What exists today:** `rollback_patches()` sends the shell script, returns `ROLLBACK_INITIATED` with command_id. That's it. No post-rollback verification.

**What to change:**
- After rollback command completes, run `rpm -qa <packages>` to verify versions reverted
- Compare against pre-patch versions (from dry-run data or pre-patch snapshot from Priority 2)
- Run `check_instance_health()` and `check_cloudwatch_alarms()` post-rollback
- Return verification result: which packages reverted successfully, which didn't, health status
- Could be a new tool `verify_rollback()` or folded into `rollback_patches()` with a `wait_and_verify=True` param

**Effort:** Low (1-2 hours). Builds on dry-run and delta data from Priorities 1-2.

---

## Priority 4: Fleet-Wide CVE Impact Assessment ✅ BUILT
**Type:** REPLACEMENT of `get_resource_dependencies` in `vulnerability_agent.py`

**Why:** `get_resource_dependencies()` checks if an instance is behind an ALB and reads tags. It's called per-instance by the vulnerability analyst. The output is always LOW for standalone instances. It answers "what infrastructure is this instance connected to?" but not "how widespread is this CVE across my fleet?" — which is what operators actually need.

**What exists today:** `get_resource_dependencies(resource_id)` → queries AWS Config for one instance, checks ALB/target group relationships, reads criticality/team/compliance tags. Returns impact_level (HIGH/MEDIUM/LOW) based on load balancer attachment. Called once per affected instance in vulnerability_agent.py.

**What to build:**
- New tool: `assess_fleet_impact(cve_id: str) -> dict`
- Query Inspector findings for the CVE across ALL environments in one call
- Group affected instances by environment
- Cross-reference with maintenance windows (`get_maintenance_windows`) and SLA policy
- Generate phased rollout recommendation with timeline
- Returns: per-environment affected count, SLA deadlines, recommended rollout order

**What to remove:** `get_resource_dependencies` from `tools.py`, `vulnerability_agent.py`, and `patch_manager.py` tool lists. Replace "Exposure" column in vuln agent output with fleet-wide impact summary.

**Effort:** Medium (3-4 hours). Combines existing Inspector query + maintenance window + SLA calculation into a single fleet-wide view.

---

## Priority 5: Package Dependency Chain Analysis
**Type:** NEW

**Why:** When patching `openssl`, operators don't know what else depends on it. If `httpd` depends on `openssl`, it may need a restart. This is the real "blast radius" — not infrastructure topology, but software dependency impact.

**What exists today:** Nothing. The agent patches packages without analyzing what depends on them.

**What to build:**
- New tool: `analyze_package_dependencies(instance_id: str, package_name: str) -> dict`
- SSM command: `rpm -q --whatrequires <package>` + `systemctl list-units --type=service --state=running`
- Cross-reference dependencies with running services
- Flag services that need restart after patching
- Return: dependency tree, affected running services, restart recommendations

**Effort:** Medium (3-4 hours). SSM commands + output parsing + service cross-reference.

---

## Priority 6: Historical Compliance Trending
**Type:** ENHANCEMENT of `query_compliance_reports` in `tools.py`

**Why:** `query_compliance_reports()` fetches reports from S3 and calculates point-in-time statistics (breach rate, severity breakdown). But it doesn't do time-series analysis — no week-over-week trends, no regression detection.

**What exists today:** `query_compliance_reports(days_back, severity, environment, sla_breaches_only, limit)` → fetches S3 objects by date prefix, reads metadata, calculates `_calculate_report_statistics()` which gives totals and breakdowns. The compliance analyst prompt says "Trends: current period vs previous" but the tool doesn't actually support period comparison — the analyst would have to call the tool twice with different date ranges.

**What to change:**
- Add `compare_period: bool = False` param to `query_compliance_reports`
- When true, also fetch the previous period (same duration) and calculate deltas
- Add to statistics: `trend` section with period-over-period changes, regression flags
- New optional tool: `get_compliance_trends(environment: str, period: str, granularity: str) -> dict` for dedicated time-series view
- Optional: dashboard UI chart component

**Effort:** Medium-high (4-6 hours). S3 query + aggregation logic + optional UI.

---

## Implementation Order

Recommended sequence:

1. **Patch dry-run** (NEW) — highest value, lowest effort, immediate workflow integration
2. **Post-patch compliance delta** (ENHANCEMENT) — completes the before/after story
3. **Rollback verification** (ENHANCEMENT) — builds on data from 1 and 2
4. **Fleet-wide CVE impact** (REPLACEMENT) — replaces `get_resource_dependencies`
5. **Package dependency chain** (NEW) — real blast radius analysis
6. **Historical trending** (ENHANCEMENT) — dashboard/reporting polish

Priorities 1-3 together give you a complete "preview → execute → verify" loop that doesn't exist today. Priority 4 replaces the weakest existing tool with something operators actually need.

---

## What to Remove When Done

After Priority 4 is implemented:
- ✅ Deleted `get_resource_dependencies` from `tools.py`
- ✅ Removed `config_client` (AWS Config) — no longer needed
- ✅ Removed from `vulnerability_agent.py` tool list and prompt — replaced with `assess_fleet_impact`
- ✅ Already removed from `patch_manager.py` tool list (done in Priority 1-3)
- ✅ Updated supervisor routing: added "fleet impact", "impact assessment", "how widespread"
- ✅ Replaced "Exposure" column with fleet impact summary in vuln agent output
