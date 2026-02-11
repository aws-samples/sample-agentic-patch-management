# Project Progress

**Last Updated**: February 11, 2026
**Status**: Pilot-ready, end-to-end tested

---

## What's Built and Working ✅

### Web UI (Operations Console)
- Dashboard: Environment cards, vulnerability table, activity feed, compliance summary, explainability table
- Chat panel: Conversational agent with SSE streaming, tool indicators, approval buttons
- Real-time workflow tracker: SQS FIFO event transport, per-environment step progress, instance status, ETA
- Collapsible two-column layout: Dashboard (72%) + Chat with smooth CSS transition
- Error boundaries: Isolated crash recovery for dashboard and chat panels
- Click-to-action: Dashboard elements pre-fill chat input
- Unified Inter font across all components

### Core Platform
- 4 agents: Supervisor, Vulnerability Analyst, Patch Manager, Compliance Analyst
- 12 AWS tools with structured error responses and full pagination
- SLA-aware patching (PCI-DSS, SOC2, HIPAA) with maintenance window integration
- Phased rollout (dev → staging → prod) with approval gates
- Two-layer health checks (SSM + CloudWatch alarms)
- Rollback via yum history undo with health verification
- Chat client with SSE streaming and real-time tool indicators
- 15 EC2 instances across 3 environments with maintenance windows
- Optimized agent prompts (~60% reduction, redundancies removed)

### Impact Assessment (formerly "blast radius")
- `get_resource_dependencies()` queries AWS Config for target groups and load balancers
- Checks `describe_target_health` to determine if other healthy targets exist
- Returns `safe_to_patch: bool`, `impact_level`, `impact_reason`, `target_group_health[]`
- Practical safety check rather than aspirational scoring

### Decision Audit Trail
- `DecisionLogger` class in `agent/helper/decision_logger.py`
- Logs routing, SLA, and error recovery decisions to CloudWatch + S3
- S3 path: `decisions/YYYY/MM/DD/{agent}/{uuid}.json`
- Integrated into Supervisor (3 routing functions)

### Structured Error Recovery
- `classify_error()` in `agent/helper/error_handling.py`
- Categories: RETRYABLE, SKIP, ABORT, UNKNOWN
- All 12 tools return structured error responses
- Patch Manager + Supervisor prompts include recovery guidance
- Verified in production: agent hit ABORT error and recovered correctly

### Workflow Event Transport (SQS FIFO)
- `WorkflowEventEmitter` in `agent/helper/workflow_events.py`
- SQS FIFO queue: `patch-automation-workflow-events.fifo`
- Agent hooks emit structured events (discovery, patch execute, health check, etc.)
- FastAPI SSE endpoint drains queue and injects events into browser stream
- React `useWorkflowState` reducer drives `WorkflowTracker` UI component

### Pagination Fixes (Data Accuracy)
- Inspector `list_findings`: paginated via `nextToken` (was capped at 100)
- EC2 `describe_instances`: paginated in `get_patch_compliance` and dashboard
- SSM `describe_instance_information`: paginated in dashboard
- SSM `list_command_invocations`: paginated in `generate_compliance_report`
- S3 `list_objects_v2`: month-prefix strategy for report fetching

### Compliance Reporting (Bug Fixes)
- `decision-type` S3 metadata now written
- `sla-met` writes `UNKNOWN` instead of `None` string
- `sla_breaches_only` filter only includes confirmed breaches
- Statistics include `by_decision` and `sla_unknown` breakdowns
- Severity/CVSS/SLA data passed correctly from Patch Manager prompt
- Verified against 29 real reports in S3

### Infrastructure Fixes
- S3 bucket policy: `grantReadWrite` instead of `addToResourcePolicy`
- IAM: Added `iam:CreateServiceLinkedRole` + `iam:PassRole` for scheduled patching
- Hardcoded role ARN fixed in `security-stack.ts`
- "test" environment added to `config.json` aliases and dashboard `ENVIRONMENTS` list

### End-to-End Test Results (Feb 9, 2026)
- Agent found HIGH CVEs across 5 dev instances
- Made EMERGENCY decision (72hr PCI-DSS SLA vs 168hr window)
- All 5 instances patched successfully
- 29 compliance reports generated across dev/staging/prod
- Metadata verified: `decision-type: EMERGENCY`, `sla-met: True`

---

## What We Evaluated and Decided NOT to Build

- **Outcome Validation** — redundant (existing tools validate outcomes)
- **Explainability Layer** — already covered by inline reasoning, decision logger, X-Ray
- **Mermaid Visualization** — terminal can't render diagrams
- **Constraints.json** — SSM handles constraints (maintenance windows, patch baselines)
- **get_recent_decisions tool** — external tools query S3/CloudWatch directly

---

## Known Limitations
- Manual approval between environments
- Linux-only rollback (yum)
- Single region (us-east-1)
- CloudWatch alarms required for application health checks

## Technical Debt
- Generic exception handlers in internal helper functions (non-tool-level) — acceptable

---

## Git History (Recent)
- `9a10685` — Refactored blast_radius → impact assessment with ELB target health checks
- `bd5e832` — Optimized all 4 agent prompts (~60% reduction)
- `009ad3c` — Fixed 8 data discrepancies (pagination, environment normalization, SLA unions)
- `6c97683` — Fixed KPI "200 total" display bug (used severity_counts sum)
- `46e60c2` — Fixed Inspector pagination (was capped at 100 findings)
- `9e1ca8a` — Added cross-reference instruction for Inspector vs SSM instance count discrepancy
- `f5542c0` — Created test plan (74 test cases across 6 areas)
- `da91eb9` — Unified Inter font across entire UI
- `5b3fcc6` — SQS FIFO workflow event transport
- `8a71bf9` — Error handling, decision audit trail, compliance fixes, infra fixes
- `27b9bf8` — Error handling, compliance reporting fixes, security stack fixes
