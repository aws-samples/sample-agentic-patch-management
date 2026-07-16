# Observability — Intelligent Patch Automation

Four layers of observability, each serving a different debugging need. Start with the layer that matches your question, then drill deeper as needed.

| Layer | What it captures | Where it lives | When to use |
|---|---|---|---|
| **1. Generative AI Dashboard & Traces** | Execution graph: which tools were called, in what order, how long, success/fail, token usage | CloudWatch Generative AI Observability, Transaction Search (`aws/spans`) | "What did the agent do?" -- trace a session, find slow tools, spot errors |
| **2. Tool Summaries** | Structured data points: input params, result counts, decisions, comparison outcomes | CloudWatch runtime-logs (`[TOOL:<name>]` prefix) | "What data drove the decision?" -- find specific counts, statuses, SLA calculations |
| **3. Full Conversation** | Complete model responses including all tool data and agent analysis | CloudWatch runtime-logs (`[runtime-logs]` streams) | "What exactly did the agent say/see?" -- full context, complete tool payloads |
| **4. SSM & S3 Records** | Ground truth: SSM commands with parameters/targets/output, S3 compliance reports, S3 routing decisions | SSM Command History, S3 bucket | "What actually happened on the instances?" -- verify what ran, audit trail |

---

## Layer 1: Generative AI Dashboard & Traces (Start Here)

The AgentCore runtime auto-instruments all Bedrock model calls, tool executions, and memory operations via OpenTelemetry. No additional setup required.

> **Prerequisite**: Transaction Search must be enabled in CloudWatch (one-time per account). Patchy's deployment enables this automatically. Verify: CloudWatch Console -> Settings -> X-Ray traces -> Transaction Search = "Enabled". See [AgentCore Observability docs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability.html) for manual setup.

**Generative AI Observability Dashboard**:

CloudWatch Console -> [Generative AI Observability](https://console.aws.amazon.com/cloudwatch/home#/genai-observability) -> Bedrock AgentCore tab

- **Agents View**: Select `intelligent_patch_automation` -> runtime metrics, sessions, traces for this agent
- **Sessions View**: Browse conversation sessions. Each shows the full sequence of model calls, tool invocations, and memory operations. Use this to replay a specific user interaction.
- **Traces View**: Inspect individual traces with span-level detail. The trace trajectory shows the execution graph — tool calls -> model invocations -> memory operations.

**Transaction Search** (for tracing specific requests):

CloudWatch Console -> Transaction Search -> filter by service name `intelligent_patch_automation.DEFAULT`

Select a trace to see the full execution graph with timing for each span.

**Metrics** (for performance and health monitoring):

| Namespace | Key Metrics |
|---|---|
| `bedrock-agentcore` | `strands.tool.call_count`, `strands.tool.duration`, `strands.tool.success_count` (per tool name), `strands.event_loop.cycle_count/duration` |
| `AWS/Bedrock-AgentCore` | `Invocations`, `Sessions`, `Duration`, `Errors`/`UserErrors`/`SystemErrors`, `Throttles`, `CPUUsed-vCPUHours`/`MemoryUsed-GBHours` |
| `AWS/Bedrock` | `InputTokenCount`/`OutputTokenCount`, `InvocationLatency`/`TimeToFirstToken`, `InvocationClientErrors` |

---

## Layer 2: Tool Summaries (Structured Debugging)

Every tool logs a structured summary at `INFO` level with a `[TOOL:<name>]` prefix. These appear in the runtime-logs streams and are searchable via CloudWatch Logs Insights.

Example log entries:
```
[TOOL:capture_patch_state] instances=5 instance_ids=['i-0d62db459b5311d39', ...]
[TOOL:capture_patch_state] RESULT: instances=5 total_missing=33 total_installed=1 no_data=0
[TOOL:verify_rollback] COMPARISON i-0d62db459b5311d39: basis=post_patch reverted=True
[TOOL:verify_rollback] RESULT: status=VERIFIED recommendation=ROLLBACK_CONFIRMED
[TOOL:patch_dry_run] RESULT: total_missing=1 instances=5 severity_filter=None override_applied=False
[TOOL:execute_patch_operation] mode=immediate env=staging instances=5 severity_filter=None
```

**Search tool logs:**
```bash
# Find your log group
LOG_GROUP=$(aws logs describe-log-groups \
  --query "logGroups[?contains(logGroupName, 'intelligent_patch_automation')].logGroupName" \
  --output text | head -1)

# Search for a specific tool's activity
aws logs start-query \
  --log-group-name "$LOG_GROUP" \
  --start-time $(python3 -c "import time; print(int(time.time() - 3600))") \
  --end-time $(python3 -c "import time; print(int(time.time()))") \
  --query-string 'fields @timestamp, body | filter body like /TOOL:capture_patch_state/ | sort @timestamp desc | limit 20'

# Search for all tool results in a time window
aws logs start-query \
  --log-group-name "$LOG_GROUP" \
  --start-time $(python3 -c "import time; print(int(time.time() - 3600))") \
  --end-time $(python3 -c "import time; print(int(time.time()))") \
  --query-string 'fields @timestamp, body | filter body like /TOOL:/ and body like /RESULT:/ | sort @timestamp asc | limit 50'

# Search for errors across all tools
aws logs start-query \
  --log-group-name "$LOG_GROUP" \
  --start-time $(python3 -c "import time; print(int(time.time() - 3600))") \
  --end-time $(python3 -c "import time; print(int(time.time()))") \
  --query-string 'fields @timestamp, body | filter body like /TOOL:/ and body like /ERROR/ | sort @timestamp desc | limit 20'
```

> **Note**: The OTEL auto-instrumentation wraps log messages in structured JSON. The actual message is in the `body` field. Use CloudWatch Logs Insights (which parses JSON automatically) rather than `--filter-pattern` for reliable searching.

---

## Layer 3: Full Conversation (Deep Investigation)

The runtime-logs streams capture the complete model responses -- including all tool data the agent received and its full analysis. This is the richest data source for post-incident forensics.

```bash
# Tail the most recent runtime-logs stream (live)
aws logs tail "$LOG_GROUP" \
  --log-stream-name-prefix "$(date -u +%Y/%m/%d)/[runtime-logs]" --follow

# List available runtime-logs streams
aws logs describe-log-streams \
  --log-group-name "$LOG_GROUP" \
  --log-stream-name-prefix "$(date -u +%Y/%m/%d)/[runtime-logs]" \
  --order-by LastEventTime --descending --limit 5 \
  --query 'logStreams[*].logStreamName' --output text

# Read a specific stream (replace with actual stream name)
aws logs get-log-events \
  --log-group-name "$LOG_GROUP" \
  --log-stream-name "<stream-name>" \
  --limit 50 --output json \
  | python3 -c "
import json, sys
for ev in json.load(sys.stdin).get('events', []):
    msg = ev.get('message', '')[:300]
    print(msg)
    print()
"
```

The runtime-logs also contain:
- `[SUPERVISOR]` prefixed entries -- routing decisions, context enrichment, session lifecycle
- `[MEMORY]` prefixed entries -- memory creation, retrieval, session management
- Strands debug output -- model call details, event loop cycles, retry state

---

## Layer 4: SSM & S3 Records (Ground Truth)

For verifying what actually happened on instances (independent of what the agent reported):

**SSM Command History:**
```bash
# List all patch commands (scans, installs, rollbacks)
aws ssm list-commands \
  --filter "key=DocumentName,value=AWS-RunPatchBaseline" \
  --max-items 20 --output json \
  | python3 -c "
import json, sys
for cmd in sorted(json.load(sys.stdin)['Commands'], key=lambda c: c['RequestedDateTime']):
    print(f\"{cmd['RequestedDateTime']} | {cmd['Parameters'].get('Operation',['?'])[0]:7s} | \
Override: {'YES' if 'BaselineOverride' in cmd['Parameters'] else 'NO ':3s} | \
{cmd['Status']:8s} | {cmd.get('Comment','')[:60]}\")"

# Get command output for a specific instance
aws ssm get-command-invocation \
  --command-id "<command-id>" \
  --instance-id "<instance-id>" \
  --output text

# Check current patch state on an instance
aws ssm describe-instance-patch-states \
  --instance-ids <instance-id> --output table
```

**S3 Compliance Reports:**
```bash
# List recent compliance reports
aws s3 ls s3://patch-compliance-reports-<account-id>/reports/ --recursive | tail -20

# Read a specific report
aws s3 cp s3://patch-compliance-reports-<account-id>/reports/<path>.json - | python3 -m json.tool
```

**S3 Compliance Reports:**
```sql
-- Query with Athena for fleet-wide audit
SELECT report_id, timestamp, operator, cve_id, environment, decision, sla_met
FROM patch_compliance_reports
WHERE operator LIKE '%jane.doe%'
ORDER BY timestamp DESC
```

---

## Debugging Workflow

When investigating an issue, follow this path:

1. **Generative AI Dashboard -> Sessions View**: Find the session by time range. See the execution graph -- which tools were called, did any fail?
2. **Tool Summaries**: Search for `[TOOL:<name>] RESULT:` in Logs Insights to see the key data points (counts, statuses, comparison outcomes).
3. **Full Conversation**: If you need the complete context, read the runtime-logs stream for that time window -- the model's full response includes all tool data.
4. **SSM/S3 Records**: If you need to verify what actually ran on instances (independent of the agent), check SSM command history and S3 compliance reports.

---

## Multi-Account: Observing Spoke Activity

Patchy's patch operations fan out to spoke accounts. To observe what happened in a spoke:

```bash
# Check what SSM commands ran in a spoke account
SPOKE_SESSION=$(aws sts assume-role --role-arn arn:aws:iam::<SPOKE_ACCOUNT>:role/PatchySpokeRole \
  --role-session-name debug --query Credentials --output json)

# List recent patch commands in the spoke
aws ssm list-commands \
  --filter "key=DocumentName,value=AWS-RunPatchBaseline" \
  --max-items 10 --output table \
  --region us-east-1
```

For centralized monitoring without switching accounts, configure [CloudWatch cross-account observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-cross-account.html) to link spoke accounts to your hub.

## Cost: Understanding Patchy's Spend

Each operator request triggers 1+ Bedrock model iterations (one per tool call cycle). Typical cost:

| Operation | Model iterations | Estimated cost |
|-----------|-----------------|---------------|
| "Show instances in dev" | 2 | ~$0.01 |
| "Patch CVE-X in prod" | 3-4 | ~$0.03 |
| "Plan staged rollout" | 4-5 | ~$0.04 |
| "Check status" | 2 | ~$0.01 |

**To monitor spend:**
```bash
# Monthly Bedrock cost for this solution
aws ce get-cost-and-usage \
  --time-period Start=$(date -u -d '-30 days' +%Y-%m-%d),End=$(date -u +%Y-%m-%d) \
  --granularity MONTHLY \
  --filter '{"Dimensions":{"Key":"SERVICE","Values":["Amazon Bedrock"]}}' \
  --metrics BlendedCost --output table
```

Set a budget alert: AWS Budgets → Create → filter Service = Amazon Bedrock → set monthly threshold.

---

## Extending Observability

If you add new tools, follow this pattern for consistent logging:
```python
logger.info(f"[TOOL:my_new_tool] param1={param1} param2={param2}")
# ... tool logic ...
logger.info(f"[TOOL:my_new_tool] RESULT: key_metric={value} status={status}")
```

The `[TOOL:<name>]` prefix makes all tool logs searchable via a single Logs Insights query pattern. The OTEL auto-instrumentation automatically correlates these with the trace spans.

### Important: Logging Configuration

All agent code must use Python `logging` module, not `print()` to stderr. AgentCore's OTEL collector captures Python logging output; raw `print()` to stderr is NOT captured by the OTEL log pipeline.

```python
import logging
logger = logging.getLogger(__name__)
logger.info("This is captured by OTEL")   # Correct
print("This is NOT captured by OTEL")      # Wrong
```
