# Troubleshooting — Intelligent Patch Automation

Common issues and how to work through them.

## Contents

- [What happens when tags are missing](#what-happens-when-tags-are-missing)
- [Fleet discovery issues](#fleet-discovery-issues)
- [Debugging agent behaviour via runtime logs](#debugging-agent-behaviour-via-runtime-logs)

---

## What happens when tags are missing

| Missing tag | Effect |
|-------------|--------|
| `PatchAutomation=enabled` | The instance is excluded from every operation. The agent refuses to target it and returns an `InstanceOutOfScope` error. |
| `Environment` | The instance shows up in the fleet, but environment-scoped operations (like "patch all dev") skip it. |
| `SLA-<severity>` | Falls back to the global default (24/72/168/720h, defined in `agent/helper/tools/_shared.py`). The compliance report records `sla_source: default` so auditors can see the decision was made without an explicit policy. |
| `ComplianceFrameworks` | The compliance dashboard's Framework column is blank for that report. The SLA decision is unaffected. |
| `Team` / `Product` / `Owner` | Recorded as `unknown`. Compliance breakdowns by team get an `unknown` bucket. |

### Where these tags come from in production

- AWS Organizations tag policies — enforce a required set of tags org-wide.
- Service Catalog / CloudFormation / CDK — bake tags into the launch template so new instances arrive correctly tagged.
- Lambda + EventBridge — listen for `RunInstances` events and apply tags from your CMDB at launch time.
- CMDB sync — a periodic job that reads ServiceNow / Device42 / etc. and reconciles EC2 tags.

The solution reads tags at runtime, so updates land within the next fleet-cache TTL (5 minutes).

> The sample environment (`./sample-env.sh deploy`) creates instances with every tag pre-populated, including realistic per-environment compliance framework and SLA combinations. See `infra/lib/sample/sample-environment-stack.ts` for the exact values.

---

## Fleet discovery issues

If instances are missing from the dashboard, or the agent can't find them, work through these in order.

### Step 1: Confirm the Resource Data Sync exists

```bash
aws ssm list-resource-data-sync --sync-type SyncFromSource \
  --query 'ResourceDataSyncItems[?SyncName==`patchy-fleet-sync`].{Name:SyncName,State:SyncSource.State,Regions:SyncSource.SourceRegions}'
```

You want to see `State: InSync` (or `InProgress` if it's still building). If the Resource Data Sync isn't there at all, re-run `./deploy.sh` — the script creates it for you.

### Step 2: Query Explorer for instances

```bash
aws ssm get-ops-summary --sync-name patchy-fleet-sync \
  --result-attributes TypeName=AWS:EC2InstanceInformation --max-results 5
```

If this returns your instances, Explorer is working. If it comes back empty, check the upstream pipeline.

### Step 3: Verify the upstream pipeline (if Explorer is empty)

Explorer's data flows through this chain: EC2 instance → SSM Agent → AWS Config recorder → SSM OpsData → Resource Data Sync → GetOpsSummary. A break anywhere leaves instances missing.

| Check | Command | What you're looking for |
|-------|---------|------------------------|
| Config is recording | `aws configservice describe-configuration-recorder-status` | `lastStatus: SUCCESS`, `recording: true` |
| Config sees EC2 instances | `aws configservice get-discovered-resource-counts --resource-types AWS::EC2::Instance` | Count matches your fleet |
| SSM Agent is online | `aws ssm describe-instance-information --query 'length(InstanceInformationList)'` | Count > 0 |
| Instances are tagged correctly | `aws ec2 describe-instances --filters Name=tag:PatchAutomation,Values=enabled --query 'Reservations[].Instances[].InstanceId'` | Your instance IDs appear |

> The usual cause of "zero instances": AWS Config is enabled in the account but hasn't finished its first resource discovery. That takes up to 6 hours the very first time Config Recording is turned on in an account, and there's no way to speed it up — it's an AWS-side process.

### Step 4: Confirm the chat agent can reach instances directly (bypasses Explorer)

Open the UI chat and ask:

```
What instances do we have in dev?
```

If the agent returns instances but the dashboard Environments tab is empty, Explorer ingestion is still in progress. The chat agent works right away because it fans out via direct API calls to each (account, region) target — no Resource Data Sync dependency.

### Multi-account: Explorer ingestion lag

When you add spoke accounts, newly discovered instances can take a while to surface in Explorer — usually within one SSM Inventory collection cycle (default: 30 minutes, set by the Host Management Quick Setup association) plus Resource Data Sync aggregation. During that window:

| Surface | Discovery method | Sees new spoke instances? |
|---------|-----------------|--------------------------|
| Dashboard — Environments tab | Explorer (Resource Data Sync) | ❌ Not until ingested |
| Dashboard — Vulnerabilities tab | Inspector (direct fan-out) | ✅ After the Inspector scan completes (~15 min) |
| Chat — fleet overview | Explorer (Resource Data Sync) | ❌ Not until ingested |
| Chat — patch compliance, vulnerabilities, maintenance windows, patch policy | Direct API fan-out | ✅ Immediately |

The dashboard shows a "warming up" banner only when Explorer returns zero entities (a completely fresh Resource Data Sync). Once the hub account's instances are ingested, the banner clears — even if spoke instances are still pending.

To check whether a specific spoke account has been ingested:

```bash
aws ssm get-ops-summary --sync-name patchy-fleet-sync \
  --result-attributes TypeName=AWS:EC2InstanceInformation \
  --filters "Key=AWS:EC2InstanceInformation.SourceAccountId,Values=<spoke-account-id>,Type=Equal"
```

An empty response means ingestion hasn't finished for that account yet. Wait for the spoke's SSM Inventory association to run (default: every 30 minutes) and for the Resource Data Sync to pick up the new data.

---

## Debugging agent behaviour via runtime logs

When the agent does something you didn't expect — calls the wrong tool, skips a step, returns an odd response — the runtime logs in CloudWatch show you exactly what it was thinking at each step.

### Where to find the logs

Log group: `/aws/vendedlogs/bedrock-agentcore/patchy_patchy/application_logs`

This needs `ENABLE_RUNTIME_LOGS=true` in `.env`. If the log group doesn't exist, redeploy with it enabled: `./deploy.sh agent`.

### What to look for

The agent's "thinking" shows up in a handful of log patterns:

1. Tool selection — which tool it decided to call, and with what parameters:
```
[TOOL:get_maintenance_windows] environment=prod instance_ids=5 targets=6
```
This tells you which tool fired, the arguments, and the scope. If the agent called the wrong tool, this is where you catch it.

2. Tool results — the data it got back:
```
[TOOL:get_maintenance_windows] RESULT: windows=3 next_execution=2025-07-03T01:00:00+00:00
[TOOL:get_patch_policy] RESULT: instances=5 with_install=3 without_policy=2
```
If the agent made a wrong SLA call, check whether the data it received was correct. A tool returning something unexpected (zero maintenance windows when you expected one, say) explains the downstream decision.

3. Steering handler interventions — when a handler corrected the agent:
```
[STEERING] PatchWorkflowSteering enabled for patch-automation-unified
```
If steering fired but the agent still misbehaved, the corrective guidance wasn't strong enough — check the specific rule in `agent/helper/steering.py`.

4. Cross-account operations — role assumptions and API calls:
```
[CROSS_ACCOUNT] Assumed role in 111111111111/us-east-1
[API:list_associations] 111111111111/us-east-1 cache MISS — calling ssm:ListAssociations
```
If a spoke account is unreachable or throwing errors, these lines show which target failed and why.

### How to investigate a specific session

```bash
# Tail the last 30 minutes of logs
aws logs tail /aws/vendedlogs/bedrock-agentcore/patchy_patchy/application_logs \
  --since 30m --format short

# Search for a specific operator's activity
aws logs start-query \
  --log-group-name /aws/vendedlogs/bedrock-agentcore/patchy_patchy/application_logs \
  --start-time $(python3 -c "import time; print(int(time.time() - 3600))") \
  --end-time $(python3 -c "import time; print(int(time.time()))") \
  --query-string 'fields @timestamp, @message | filter @message like /jane@example.com/ or @message like /\[TOOL:/ | sort @timestamp asc | limit 200'
```

### Common patterns

| Symptom | What to check in the logs |
|---------|----------------------|
| Agent didn't check maintenance windows before patching | Search for `[TOOL:get_maintenance_windows]` — if it's missing, the steering handler didn't fire or the model skipped it |
| Agent chose EMERGENCY when it should have been SCHEDULED | Search for `[TOOL:get_patch_policy]` RESULT — check whether `with_install=0` (no policy found, which triggers emergency) |
| Agent patched the wrong instances | Search for `[TOOL:resolve_execution_scope]` or `[TOOL:execute_patch_operation]` — check the `instance_ids` parameter |
| Agent didn't find spoke instances | Search for `[CROSS_ACCOUNT]` — look for AccessDeniedException or failed role assumptions |
| Agent asked for confirmation but didn't proceed after "yes" | Search for `ConfirmationGoalHandler` — if it's absent, the handler didn't fire. Check `agent/helper/goals.py`. |
| Agent returned an empty or generic response | Search for `[TOOL:get_response_template]` — the template tool may have returned a format the model couldn't fill |
