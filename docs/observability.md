# Observability — Intelligent Patch Automation

There are four layers of observability here, each answering a different question. Start with the one that matches what you're asking, and drill down from there.

| Layer | What it captures | Where it lives | When to use |
|---|---|---|---|
| 1. Generative AI Dashboard & Traces | The execution graph: which tools were called, in what order, how long they took, pass/fail, token usage | CloudWatch Generative AI Observability, Transaction Search (`aws/spans`) | "What did the agent do?" — trace a session, find slow tools, spot errors |
| 2. Tool Summaries | Structured data points: input params, result counts, decisions, comparison outcomes | CloudWatch runtime-logs (`[TOOL:<name>]` prefix) | "What data drove the decision?" — find specific counts, statuses, SLA calculations |
| 3. Full Conversation | Complete model responses, including all tool data and the agent's analysis | CloudWatch runtime-logs (`[runtime-logs]` streams) | "What exactly did the agent say and see?" — full context, complete tool payloads |
| 4. SSM & S3 Records | Ground truth: SSM commands with parameters/targets/output, S3 compliance reports | SSM Command History, S3 bucket | "What actually happened on the instances?" — verify what ran, audit trail |

---

## Layer 1: Generative AI Dashboard & Traces (start here)

The AgentCore runtime auto-instruments every Bedrock model call, tool execution, and memory operation through OpenTelemetry. Nothing to set up on your end.

> Prerequisite: Transaction Search has to be enabled in CloudWatch (a one-time thing per account). Set `ENABLE_TRACING=true` in `.env` and redeploy — `deploy.sh` turns it on via `ensure_observability()`. Without that env var, tracing is off by default. To verify: CloudWatch Console → Settings → X-Ray traces → Transaction Search = "Enabled". See the [AgentCore Observability docs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability.html) for manual setup.

Generative AI Observability Dashboard:

CloudWatch Console → [Generative AI Observability](https://console.aws.amazon.com/cloudwatch/home#/genai-observability) → Bedrock AgentCore tab

- Agents View: select `patchy` for runtime metrics, sessions, and traces for this agent.
- Sessions View: browse conversation sessions. Each one shows the full sequence of model calls, tool invocations, and memory operations — use it to replay a specific interaction.
- Traces View: inspect individual traces at span-level detail. The trace trajectory shows the execution graph — tool calls → model invocations → memory operations.

Transaction Search (for tracing specific requests):

CloudWatch Console → Transaction Search → filter by service name `patchy_patchy.DEFAULT`

Pick a trace to see the full execution graph with per-span timing.

Metrics (for performance and health):

| Namespace | Key Metrics |
|---|---|
| `bedrock-agentcore` | `strands.tool.call_count`, `strands.tool.duration`, `strands.tool.success_count` (per tool name), `strands.event_loop.cycle_count/duration` |
| `AWS/Bedrock-AgentCore` | `Invocations`, `Sessions`, `Duration`, `Errors`/`UserErrors`/`SystemErrors`, `Throttles`, `CPUUsed-vCPUHours`/`MemoryUsed-GBHours` |
| `AWS/Bedrock` | `InputTokenCount`/`OutputTokenCount`, `InvocationLatency`/`TimeToFirstToken`, `InvocationClientErrors` |

---

## Layer 2: Tool Summaries (structured debugging)

Every tool logs a structured summary at `INFO` level with a `[TOOL:<name>]` prefix. These land in the runtime-logs streams and are searchable through CloudWatch Logs Insights.

Sample entries:

```
[TOOL:capture_patch_state] instances=5 instance_ids=['i-0d62db459b5311d39', ...]
[TOOL:capture_patch_state] RESULT: instances=5 total_missing=33 total_installed=1 no_data=0
[TOOL:verify_rollback] COMPARISON i-0d62db459b5311d39: basis=post_patch reverted=True
[TOOL:verify_rollback] RESULT: status=VERIFIED recommendation=ROLLBACK_CONFIRMED
[TOOL:patch_dry_run] RESULT: total_missing=1 instances=5 severity_filter=None override_applied=False
[TOOL:execute_patch_operation] mode=immediate env=staging instances=5 severity_filter=None
```

Searching tool logs:

```bash
# Find your log group (the runtime-id is derived from your agent name)
LOG_GROUP="/aws/vendedlogs/bedrock-agentcore/patchy_patchy/application_logs"

# Or discover it dynamically:
LOG_GROUP=$(aws logs describe-log-groups \
  --query "logGroups[?contains(logGroupName, 'patchy')].logGroupName" \
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

> Note: the OTEL auto-instrumentation wraps log messages in structured JSON, so the actual message sits in the `body` field. Use CloudWatch Logs Insights (which parses JSON automatically) rather than `--filter-pattern` for reliable searching.

---

## Layer 3: Full Conversation (deep investigation)

The runtime-logs streams capture the complete model responses — all the tool data the agent received, plus its full analysis. This is the richest source for post-incident forensics.

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

# Read a specific stream (replace with the actual stream name)
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

The runtime-logs also carry:

- `[AGENT]` entries — request lifecycle, fleet context enrichment, session info
- `[MEMORY]` entries — memory creation, retrieval, session management
- Strands debug output — model call details, event loop cycles, retry state

---

## Layer 4: SSM & S3 Records (ground truth)

When you want to verify what actually happened on the instances, independent of what the agent reported:

SSM Automation History (primary — the agent uses Automation, not direct SendCommand):

```bash
# List recent Automation executions for Patchy documents
aws ssm describe-automation-executions \
  --filters Key=DocumentNamePrefix,Values=Patchy- \
  --max-items 20 --output table

# Get step-level detail for a specific execution
aws ssm describe-automation-step-executions \
  --automation-execution-id <execution-id> --output table
```

SSM Command History (secondary — commands run inside Automation steps):

```bash
# List all patch commands (these are internal to the Automation document's aws:runCommand step)
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

S3 Compliance Reports:

```sql
-- Query with Athena for a fleet-wide audit
SELECT report_id, timestamp, operator, cve_id, environment, decision, sla_met
FROM patch_compliance_reports
WHERE operator LIKE '%jane.doe%'
ORDER BY timestamp DESC
```

---

## Debugging workflow

When you're chasing down an issue, this path usually gets you there fastest:

1. Generative AI Dashboard → Sessions View: find the session by time range and look at the execution graph. Which tools were called, and did any fail?
2. Tool Summaries: search for `[TOOL:<name>] RESULT:` in Logs Insights to see the key data points (counts, statuses, comparison outcomes).
3. Full Conversation: if you need the complete context, read the runtime-logs stream for that time window — the model's full response includes all the tool data.
4. SSM/S3 Records: if you need to confirm what actually ran on the instances (independent of the agent), check the SSM command history and the S3 compliance reports.

---

## Multi-account: observing spoke activity

Patch operations fan out to spoke accounts. To see what happened in one:

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

For centralized monitoring without hopping between accounts, set up [CloudWatch cross-account observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-cross-account.html) to link spoke accounts to your hub.

## Cost: where the spend goes

Each operator request triggers one or more Bedrock model invocations on Claude Sonnet 5. Cost scales with the number of agentic-loop cycles (each cycle is one model call plus the tool it triggers) and the size of the context and tool results. As a rough guide:

| Operation | Agentic-loop cycles |
|-----------|--------------------|
| Simple query ("show instances in dev", "check status") | 1–2 |
| Vulnerability lookup + fleet impact | 2–4 |
| Patch workflow (scan → confirm → execute → verify) | 4–6 |
| Staged rollout across environments | 6+ |

> Cycle counts move around with conversation length (more context means more input tokens) and tool result size. For real numbers, pull the token counts from the CloudWatch metrics `AWS/Bedrock` → `InputTokenCount` / `OutputTokenCount` and multiply by the [Claude Sonnet pricing](https://aws.amazon.com/bedrock/pricing/) for your region.

To monitor spend:

```bash
# Monthly Bedrock cost for this solution
aws ce get-cost-and-usage \
  --time-period Start=$(date -u -d '-30 days' +%Y-%m-%d),End=$(date -u +%Y-%m-%d) \
  --granularity MONTHLY \
  --filter '{"Dimensions":{"Key":"SERVICE","Values":["Amazon Bedrock"]}}' \
  --metrics BlendedCost --output table
```

Set a budget alert: AWS Budgets → Create → filter Service = Amazon Bedrock → set a monthly threshold.

---

## Extending observability

When you add a new tool, follow this pattern so the logging stays consistent:

```python
logger.info(f"[TOOL:my_new_tool] param1={param1} param2={param2}")
# ... tool logic ...
logger.info(f"[TOOL:my_new_tool] RESULT: key_metric={value} status={status}")
```

The `[TOOL:<name>]` prefix keeps every tool log searchable through a single Logs Insights query pattern, and the OTEL auto-instrumentation correlates these with the trace spans automatically.

### One thing to get right: logging configuration

All agent code has to use the Python `logging` module, not `print()` to stderr. AgentCore's OTEL collector captures Python logging output; a raw `print()` to stderr is not captured by the OTEL log pipeline.

```python
import logging
logger = logging.getLogger(__name__)
logger.info("This is captured by OTEL")   # Correct
print("This is NOT captured by OTEL")      # Wrong
```
