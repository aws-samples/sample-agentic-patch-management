# System Architecture

## Agent System

```
┌─────────────────────────────────────────────────────────────┐
│                 Supervisor Agent                            │
│         (Keyword routing + OUT_OF_SCOPE fallback)           │
│         DecisionLogger: logs routing decisions              │
├─────────────┬───────────────────┬───────────────────────────┤
│ Vulnerability│   Patch Manager   │   Compliance Analyst     │
│ Analyst      │   (11 tools)      │                          │
│              │                   │                          │
│ • Inspector  │ • 7-step workflow │ • S3 report queries      │
│ • Config     │ • Error recovery  │ • SLA breach tracking    │
│ • CVSS       │ • Decision logger │ • Trend analysis         │
└─────────────┴───────────────────┴───────────────────────────┘
```

### Routing Logic (Keyword-Based)
- Security keywords (CVE, vulnerability, CVSS) → Vulnerability Analyst
- Infrastructure keywords (patch, instance, health, rollback, maintenance) → Patch Manager
- Compliance keywords (report, SLA, audit, compliance) → Compliance Analyst
- AWS service keywords (Lambda, S3, RDS) → use_aws (direct)
- OUT_OF_SCOPE response → retry with use_aws fallback

### Patch Manager 7-Step Workflow
```
Step 1: get_patch_compliance(environment, severity) → instances + SLA
Step 2: get_maintenance_windows(environment) → next window timing
Step 3: Calculate: window_hours vs sla_hours → EMERGENCY or SCHEDULED
Step 4: execute_patch_operation(execution_mode) → command_id
Step 5: check_instance_health() [if EMERGENCY]
Step 6: check_cloudwatch_alarms() [if EMERGENCY]
Step 7: generate_compliance_report(severity, cvss_score, sla_hours, sla_source)
```

## Key Patterns

### Structured Error Recovery
Tools return classified errors instead of generic `{"error": str(e)}`:
```python
{"error": "...", "error_code": "ThrottlingException", "category": "RETRYABLE", "suggestion": "...", "retryable": True}
```
Categories: RETRYABLE (retry once), SKIP (remove resource, continue), ABORT (stop workflow), UNKNOWN (report to user).
Agent follows recovery guidance in system prompt. No separate error recovery framework.

### Decision Audit Trail
`DecisionLogger` in `agent/helper/decision_logger.py`:
- Logs to CloudWatch (structured JSON, `DECISION:` prefix)
- Stores in S3: `decisions/YYYY/MM/DD/{agent}/{uuid}.json`
- Uses compliance reports bucket (`patch-compliance-reports-{account_id}`)
- Convenience methods: `log_routing_decision`, `log_sla_decision`, `log_error_recovery`
- Integrated into Supervisor (3 routing functions)

### Two-Layer Health Checks
1. Infrastructure: `check_instance_health()` → SSM DescribeInstanceInformation
2. Application: `check_cloudwatch_alarms()` → CloudWatch DescribeAlarms (StateValue=ALARM, filter by InstanceId dimension)
3. 2-minute wait for alarm evaluation before checking

### Compliance Reporting
- `generate_compliance_report()` → S3 `YYYY/MM/DD/{env}-{cve}.json`
- S3 metadata: `decision-type`, `sla-met`, `severity`, `team`, `product`
- `query_compliance_reports()` → list + download + aggregate from S3
- `sla_breaches_only` filter only includes confirmed breaches (`sla_met is False`)
- Statistics include `by_decision` and `sla_unknown` breakdowns

## Infrastructure (4 CDK Stacks)

1. **Networking** — VPC, public/private subnets, NAT Gateway
2. **Security** — Security groups, IAM roles, AWS Config, S3 compliance bucket (RETAIN policy), SQS FIFO queue for workflow events
3. **AgentCore Permissions** — IAM policies (25+ services read, SSM write, S3 write, SQS send/receive, iam:CreateServiceLinkedRole, iam:PassRole)
4. **VulnerabilityDriven** — 15 EC2 instances, maintenance windows, CloudWatch alarms

`./deploy.sh destroy` only removes stack 4 (EC2 instances). S3 bucket lives in stack 2 and is never destroyed.

### S3 Bucket Structure
```
patch-compliance-reports-{account_id}/
├── YYYY/MM/DD/{env}-{cve}.json          # Compliance reports
└── decisions/YYYY/MM/DD/{agent}/{uuid}.json  # Decision audit trail
```

## Data Flow

### Patching (Async)
```
User → Patch Manager → SSM SendCommand → returns command_id
User: "check status" → SSM GetCommandInvocation → status
User: "check health" → SSM + CloudWatch → health summary
User: "proceed" → next environment
```

### SLA Decision
```
get_patch_compliance(severity) → sla_hours (from ComplianceFrameworks tag)
get_maintenance_windows() → window_hours
IF window_hours <= sla_hours → SCHEDULED (register with window)
ELSE → EMERGENCY (patch immediately)
```

### Workflow Event Transport (SQS FIFO)
```
AgentCore Container                    Local Machine
+---------------------+               +----------------------+
| Specialist Agent    |               | FastAPI SSE Endpoint |
|   |                 |               |   |                  |
| WorkflowTrackingHook|--SQS FIFO--> | emitter.drain()      |
|   |                 |               |   |                  |
| sqs.send_message()  |               | yield evt.to_sse()   |
+---------------------+               +----------------------+
                                              |
                                        Browser (SSE)
                                              |
                                      useWorkflowState reducer
                                              |
                                      WorkflowTracker UI
```

Queue: `patch-automation-workflow-events.fifo`
MessageGroupId: session_id (per-session ordering)
Tool→step mapping in `TOOL_STEP_MAP` (workflow_events.py)

### Web UI Architecture
```
Browser (React + Cloudscape)  ←→  FastAPI (server.py)  ←→  AgentCore Runtime
                                        ↓
                                  /api/chat → SSE stream (agent + workflow events)
                                  /api/dashboard → concurrent boto3 calls (30s cache)
                                  /api/health → config validation
```

Dashboard uses direct AWS API calls (not agent) for speed and reliability.
Chat panel streams agent responses via SSE with inline workflow events.
