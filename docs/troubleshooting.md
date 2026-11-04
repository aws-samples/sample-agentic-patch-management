# Troubleshooting

What a refusal or error means and how to fix it, and where to look when the answer isn't obvious.

- [Plan refusals](#plan-refusals)
- [Approval errors](#approval-errors)
- [Run problems](#run-problems)
- [UI errors](#ui-errors)
- [Missing instances or findings](#missing-instances-or-findings)
- [Multi-account access](#multi-account-access)
- [Deploy errors](#deploy-errors)
- [Logs and traces](#logs-and-traces)

## Plan refusals

When a change can't be planned safely, the agent says why in the chat and nothing is stored or run. Each refusal has a code, visible in the tool result (`PATCHY_DEBUG_LOGS=true` logs it).

| Code | Cause | Fix |
|---|---|---|
| `ChangeFreeze` | The Change Calendar is closed, or can't be read | Wait until it opens, or fix the runtime's access to it |
| `AbortAlarmsInvalid` | `ABORT_ALARMS` names more than one alarm; SSM watches one per run | Combine the alarms in a CloudWatch composite alarm, set `ABORT_ALARMS` to it, and run `./deploy.sh agent` |
| `StageNotVerified` | `REQUIRE_STAGED_ROLLOUT=true`, and the previous environment has no verified run for the same CVE or severity in the last 14 days | Patch the earlier environment, then run `Verify health` |
| `LocationLimit` | A plan by instance ID spans more accounts and Regions than the environment allows at once (outside dev, more than one) | Patch the environment as a whole, or make one plan per account and Region |
| `UntaggedCountsAsProd` | An instance in a plan for any environment other than prod has no `Environment` tag | Tag it, or plan it as prod |
| `EnvironmentMismatch` | The request names an environment other than the instances' own tag | Plan each environment separately |
| `MixedEnvironmentTags` | Instances in one environment carry two spellings, such as `prod` and `Production` | Retag them to one value |
| `FleetViewTruncated` | The fleet has more instances than `MAX_FLEET_SIZE`, so the plan's checks can't see every instance the run would reach | Raise `MAX_FLEET_SIZE` and run `./deploy.sh agent`, or plan by instance ID |
| `ScopeUnreadable` | Some instances weren't found, and one or more accounts and Regions couldn't be read (for example, the spoke role was denied), so Patchy can't confirm they're in scope. It makes no plan | Check `PatchySpokeRole` in the named account, then try again |
| `FleetViewUnavailable` | The last fleet refresh couldn't read SSM Explorer, so the fleet view an environment plan's checks use may be out of date | It usually clears on the next refresh, within five minutes. Meanwhile, plan by instance ID, which checks each instance in EC2 directly. If it persists, check the Resource Data Sync and the runtime role's `ssm:GetOpsSummary` permission |
| `FleetViewIncomplete` | The last fleet refresh couldn't read the instances in one or more accounts and Regions, so the plan's checks can't see every instance the run would reach there | Ask again after the next refresh (within five minutes). If it persists, check `PatchySpokeRole` in that account |
| `RunInProgress`, or a run that won't start with "is still InProgress" | A Patchy run that hasn't finished covers instances this change targets. Two runs on the same instances compete for the SSM instance lock and mix their package changes, which a rollback can't separate. The check is made when planning and again when the plan starts. A run that was stopped or failed in the last three hours counts as "finishing" while its report is pending, because the patch commands it already sent may still be running; a run whose state can't be read counts too | Wait for that run to finish (the Changes view lists runs in progress), then ask again or approve again |
| `CveScopeUnreadable` | Inspector couldn't be read in every Region for the CVE, so the affected instances and the packages that fix them aren't fully known. Patchy makes no plan rather than leave out instances as unaffected | Try again in a few minutes; if one Region keeps failing, check Inspector there |
| `InspectorUnreadable` | Inspector couldn't be read for the CVE, even after retrying, so its severity and SLA aren't known | Try again in a few minutes; if it persists, check the runtime role's Inspector access and the delegated administrator setup |
| `ReportPending` | A rollback was asked for while a patch run on the same targets has no report yet, so the last reported run would be the wrong one to undo | Wait for that run's report, usually within 5 minutes of its commands finishing, then ask again |
| `ReportsUnreadable`, `ReportPendingUnknown` | Patchy couldn't read some reports, or couldn't check for unreported runs, so it can't tell which run is the last one to undo | Try again in a few minutes; if it persists, check the runtime role's access to the compliance bucket |
| `NoTargets` | A CVE plan found no in-scope instances, usually because Explorer is still syncing | See [Missing instances or findings](#missing-instances-or-findings) |
| `NoPackageFix` | Inspector lists no OS package fix for the CVE on the instances the plan targets (for example, only a language package fixes it) | Patch by severity instead. If the instances are new, check they carry the scope and `Environment` tags: a plan reads new instances from EC2, so they don't need to be in Explorer yet |
| `CveScopeUnsupported` | A CVE plan includes Windows instances | Patch by severity instead |
| `CveScopeTooLarge` | The CVE has more than 5,000 findings, so the package list could be incomplete | Patch by severity, or narrow to an account or to instance IDs |
| `NoPatchRun` | A rollback found no finished Patchy patch run in the last 30 days for the targets, or none for the accounts requested | Nothing Patchy can safely undo; roll back by hand |
| `InstanceOutOfScope` | An instance ID is wrong or terminated, lacks the scope tag, is outside the configured accounts and Regions, or its account has no spoke role | Check the ID and tags; in multi-account mode, `./deploy.sh spoke` |
| `AccountOutOfScope`, `RegionOutOfScope`, `OutOfScope` | Accounts, Regions or OUs outside the configuration. With multi-account on and no accounts or OUs configured, everything is out of scope. | Use the configured scope, or add it to `.env` and run `./deploy.sh` |
| `ScopeCheckFailed` | The scope check itself failed (throttling, a failed role assumption), so it refuses rather than guess | Ask again; if it persists, see [Multi-account access](#multi-account-access) |
| `InvalidParameter`, `InvalidInstanceId` | A malformed instance ID, or a bad window name, schedule or duration | Correct the request |

The agent can also answer that a maintenance window will patch the targets before the SLA deadline. That isn't an error: Patchy plans nothing and waits for the window. Ask to patch now if you don't want to wait.

## Approval errors

Approve and Cancel go to the UI API, which checks the stored plan before writing the decision.

| Response | Message | Cause and fix |
|---|---|---|
| 403 | "Only operators can approve or cancel plans." | The user isn't in `operators`. |
| 403 | "Only auditors can see every operator's plans." | The user isn't in `auditors`. Add them in the Cognito console; they sign in again for it to apply. |
| 403 | "The person who requested a plan can't approve it." | `REQUIRE_SEPARATE_APPROVER=true`; another operator must approve. |
| 404 | "Plan not found." | The plan doesn't exist, or belongs to another conversation. Ask again in this conversation. |
| 409 | "Plan contents changed after it was created." | The stored plan no longer matches its own hash. Don't approve it; check who can write `plans/` in the bucket. |
| 409 | "The plan changed after the card was shown. Ask for a fresh plan." | The record you saw is out of date. Ask again. |
| 409 | "Plan is already approved." / "Plan is already cancelled." / "Plan was cancelled." | A plan takes one decision. An approver whose run never started may approve again to start it. |
| 410 | "Plan expired. Ask for a fresh plan." | Plans last 15 minutes. |
| 401 | "Your sign-in has no access token. Sign in again." | `RUNTIME_AUTH=jwt` and the request had no token to forward. |
| 503 | "REQUIRE_SEPARATE_APPROVER needs Cognito sign-in to tell approvers apart." | Turn on Cognito, or unset the setting. |
| 429 | "Rate limit exceeded." | Chat, approve and stop are limited to 20 a minute per user. |

## Run problems

If the UI accepts an approval but the runtime won't run it, the run view shows "Plan could not run" with the reason, and the runtime logs `[PLAN] refused <plan id>: <reason>`.

| Reason | Meaning |
|---|---|
| `plan not found`, `invalid plan id` | No such plan, or one from another conversation |
| `plan hash does not match its contents`, `plan or approval record is malformed` | A record in the bucket was altered or can't be read |
| `plan was cancelled`, `plan is not approved` | No approval to act on |
| `approval is for a different plan hash`, `approval is for another session`, `approval was not made by an operator` | The decision doesn't match the plan |
| `plan expired before it was approved`, `plan expired` | The plan's 15 minutes ran out |
| `Another Patchy plan (...) began starting ...`, `... at the same moment` | Another patch or rollback plan was starting. Plans start one at a time, so the run-in-progress check sees the earlier run. The approval stays unused: start it again in a minute, and if the other plan covers the same instances the check then refuses it with "is still InProgress" |
| `Patchy couldn't take the start lock` | The runtime couldn't read or write `run-locks/start.json` in the compliance bucket. The approval stays unused |
| `approval is stale` | The run didn't start within 10 minutes of approval. The change record offers "Start it again" only until then, or until the plan expires if that is sooner |
| `plan has already run` | A plan runs once |
| `install list doesn't match the approved plan` | A CVE plan's package list changed since approval |
| `Change calendar ... is closed`, `... could not be read` | The calendar closed after approval, or can't be read. The approval stays unused, so the plan can still run if the calendar reopens in time. |

If the plan names a dry-run scan, making the plan and starting the run both refuse when the scan didn't succeed (`DryRunNotComplete`), couldn't be read (`DryRunVerificationFailed`), is more than two hours old (`DryRunStale`), or isn't a Patchy scan of the plan's targets (`DryRunMismatch`). Ask for a new scan and a new plan. A severity plan whose override file is missing from the bucket refuses with a message that the baseline override is missing from S3; run `./deploy.sh` to upload the files.

Other things you may see:

| Symptom | Meaning and fix |
|---|---|
| Partly started | A location failed to start after others started. The started locations carry on and can be followed and stopped. "Not started" names each remaining location and the error. The approval is spent, so plan those locations again once the cause is fixed (often the spoke role, or a quota). |
| "can't be stopped from this card" or "won't get a compliance report" | The run started, but its run record or report context couldn't be saved. Stop it in the SSM console if needed; its report won't be written. |
| A location stops straight away with `ABORT_ALARMS` set | That location has no such alarm, or the alarm is already in ALARM. Create the alarm there, or remove it from `ABORT_ALARMS`. The change record's stop alarm check shows this before approval, and SSM shows the reason in the location's execution. |
| A step shows Cancelled but the instance is patched | A stop alarm or Stop cancelled the run while that instance was patching, and the patch finished anyway. The report counts it from the command result. |
| "Run not found." | The run wasn't started from this conversation. The UI shows and stops only its own conversation's runs. |
| "Only operators can use the assistant." / "Only operators can run or stop changes." / "This conversation belongs to another user." | From the runtime in `RUNTIME_AUTH=jwt` mode: the user isn't in `operators`, or the conversation belongs to someone else. Start a new conversation. |
| Spoke instances fail with `Unable to download file from S3` and `403 ... HeadObject` in the `AWS-RunPatchBaseline` output | The instance's role can't read the hub bucket's override files. Add the grant in [Deploying: before you start](deploying.md#before-you-start) to the role in that account. |
| A rollback says "Nothing from this run on this instance" | The run made no yum change on that instance, so nothing was undone. This is a success. |

## UI errors

| Symptom | Cause | Fix |
|---|---|---|
| 401 with Cognito on | The sign-in headers are missing or failed verification, for example because the request bypassed the load balancer, or the stack changed | Use the UI through its load balancer; after a stack change, run `./deploy.sh ui` |
| The browser asks for an API key | API keys are configured without Cognito | Enter the operator or viewer key; it's kept until the tab closes |
| 403 "Cross-site request refused." or 415 | A POST from another origin, or not JSON | Call the API from the UI's own origin with `Content-Type: application/json`. For local development on another port, add it to `CORS_ORIGINS`. |
| An operator shows as "Viewer, read-only" | The task can't verify the group claim (`COGNITO_USER_POOL_ID` or `COGNITO_CLIENT_ID` missing), or the user isn't in `operators` | Check the group, then `./deploy.sh ui` |
| `/api/health` returns 503 and the task is unhealthy | The UI can't find the runtime ARN (the UI log says `[HEALTH] No AgentCore config found`) | `./deploy.sh agent` |
| "This plan isn't in this conversation" | The plan was made in another conversation, or before a reload that started a new one | Ask again |
| Refresh returns the same data | Forced refreshes are limited to 4 a minute per user | Wait a minute |
| A report hasn't appeared | Reports are written when a run ends; the 5-minute sweep catches any missed. A stopped run's report waits for the commands it had already sent (up to 3 hours), and the run stays under Running now meanwhile. A report also waits up to 3 hours when some locations' outcomes can't be read, then is written marked incomplete with those locations named | Wait 5 minutes after the last command finishes; then check the report Lambda's log |

## Missing instances or findings

The fleet comes from SSM Explorer, which can lag behind EC2 by hours. Work through these checks in order.

1. The Resource Data Sync exists:

   ```bash
   aws ssm list-resource-data-sync --sync-type SyncFromSource \
     --query 'ResourceDataSyncItems[?SyncName==`patchy-fleet-sync`].{State:SyncSource.State,Regions:SyncSource.SourceRegions}'
   ```

   `InSync` is good, and `InProgress` means it's still building. If it's missing, run `./deploy.sh`. A change to `SPOKE_REGIONS` re-creates it.
2. Explorer returns instances:

   ```bash
   aws ssm get-ops-summary --sync-name patchy-fleet-sync \
     --result-attributes TypeName=AWS:EC2InstanceInformation --max-results 5
   ```

   To check one spoke, add `--filters "Key=AWS:EC2InstanceInformation.SourceAccountId,Values=<account-id>,Type=Equal"`.
3. The pipeline behind Explorer is working:

   | Check | Command | Expect |
   |---|---|---|
   | AWS Config is recording | `aws configservice describe-configuration-recorder-status` | `recording: true` |
   | SSM Agent is online | `aws ssm describe-instance-information --query 'length(InstanceInformationList)'` | More than zero |
   | Instances are tagged | `aws ec2 describe-instances --filters Name=tag:PatchAutomation,Values=enabled --query 'Reservations[].Instances[].InstanceId'` | Your instances |

   The first discovery after AWS Config is turned on takes up to six hours. A new spoke usually appears after one SSM Inventory cycle (30 minutes by default).

While Explorer catches up, the agent can still check and patch named instances, because scope and status checks call EC2 and SSM directly. In multi-account mode, the Overview shows "Fleet data still warming up" until Explorer returns anything. If the Explorer call itself fails (for example `AccessDeniedException` or `ThrottlingException`), the Overview shows "Could not query SSM Explorer" with the error code instead, and the counts come from reading each account directly.

If spoke findings are missing, the hub isn't Inspector's delegated administrator. Patchy reads findings once per Region with the hub's credentials, so it sees only the hub's own findings. `./deploy.sh status` shows whether the hub is the delegated administrator, and Inspector's state in each Region.

## Multi-account access

| Symptom | Cause | Fix |
|---|---|---|
| The dashboard shows only the hub after a UI deploy; the UI log shows `Could not assume role in spoke ...: AccessDenied` | The spoke read role trusts the exact ARNs of the UI task role and report Lambda role, and the deploy replaced one | `./deploy.sh spoke` |
| The agent can't reach a spoke; the runtime log's `[CROSS_ACCOUNT]` line names the account | The agent was redeployed into a new runtime role, which the spoke write role doesn't trust | `./deploy.sh` |
| Overview says some accounts couldn't be read | The read role is missing in those accounts or Regions, or doesn't trust the UI; their instances aren't counted | `./deploy.sh spoke`, then check the trust |
| Findings, Changes or Reports says something couldn't be read, with an error code | The UI task role couldn't read Inspector in that Region, the reports bucket, or the pending reports. The page leaves that data out rather than showing it as empty | Check the UI task role's access for the named Region or bucket; the UI log has the full error |

To see what the read role trusts:

```bash
aws iam get-role --role-name PatchySpokeReadRole --profile <spoke-profile> \
  --query 'Role.AssumeRolePolicyDocument'
```

## Deploy errors

| Message | Fix |
|---|---|
| "Cognito sign-in needs an ACM certificate (ACM_CERTIFICATE_ARN)." | Set `ACM_CERTIFICATE_ARN`, set `ALLOW_SELF_SIGNED_CERT=true` for a demo, or set `COGNITO_ENABLED=false` |
| "ACM certificate ARN is self-signed, and the ALB is public with Cognito sign-in." | Use a trusted certificate, or `ALLOW_SELF_SIGNED_CERT=true` for a demo |
| "RUNTIME_AUTH=jwt needs the Patchy-UI Cognito pool." | Deploy once without `RUNTIME_AUTH`, then set it and run `./deploy.sh agent` |
| "Found N AgentCore role ARNs ..." / "Could not find the Patchy AgentCore runtime role" | Set `AGENTCORE_ROLE_ARN`, or deploy the agent first |
| "AgentCore CLI X is installed; this release needs 0.20.1" | Run `npm install -g @aws/agentcore@0.20.1`. To deploy with your version anyway, set `AGENTCORE_CLI_VERSION` to it |
| "This AWS CDK Toolkit is not compatible with the AWS CDK library" / "Cloud assembly schema version mismatch" | The AgentCore CLI and the agent's CDK library don't match. Install AgentCore CLI 0.20.1, and don't change `agent/agentcore/cdk/package-lock.json`: the deploy installs from it with `npm ci` |
| "Your shell sets AWS_PROFILE=..., overriding AWS_PROFILE=... in .env." | Type the account ID to confirm, unset the shell's profile, or set `PATCHY_CONFIRM_ACCOUNT` |
| "Your shell sets AWS_REGION=..., overriding AWS_REGION=... in .env." | Type the Region to confirm, unset the shell's `AWS_REGION`, or set `PATCHY_CONFIRM_REGION` |
| "Eval failed (accuracy below 80%). Deploy blocked." | Fix the routing regression, or set `SKIP_EVAL=true` to skip the gate |
| "Account N is not the management account ... or a delegated administrator for SSM." | Run the printed registration command from the management account |
| "AWS_ORG_ID is required when MULTI_ACCOUNT_ENABLED=true." | Set `AWS_ORG_ID` |
| "ABORT_ALARMS names more than one alarm." | Combine the alarms in a CloudWatch composite alarm and set `ABORT_ALARMS` to that one |
| "Patchy-SampleEnv is still deployed ..." | Run `./sample-env.sh destroy` before `./deploy.sh destroy` |
| `./deploy.sh ui` says the task definition didn't change | `rm -rf infra/cdk.out && ./deploy.sh ui` |

## Logs and traces

| What | Where | Kept |
|---|---|---|
| Agent | `/aws/bedrock-agentcore/runtimes/patchy_patchy-<id>-DEFAULT` | Not set by Patchy |
| Agent, structured per invocation (`ENABLE_RUNTIME_LOGS=true`) | `/aws/vendedlogs/bedrock-agentcore/patchy_patchy/application_logs` | 14 days |
| UI API | `/patch-automation/ui` | 14 days |
| Report Lambda | The `ReportFn` log group in `Patchy-UI` | 30 days |
| Runs | SSM Automation history, in the hub and each spoke | SSM's retention |

Find the agent's log group. A redeploy that replaces the runtime leaves the old runtime's log group behind, so take the newest:

```bash
LOG_GROUP=$(aws logs describe-log-groups --log-group-name-prefix /aws/bedrock-agentcore/runtimes/patchy_patchy \
  --query 'sort_by(logGroups, &creationTime)[-1].logGroupName' --output text)
aws logs tail "$LOG_GROUP" --follow
```

### What the agent logs

Lines look like `LEVEL | logger | message`, and most messages start with a bracketed prefix:

| Prefix | Records |
|---|---|
| `[AGENT]` | Each turn's operator and session, and the first 80 characters of the message |
| `[PLAN]` | Plan created, claimed (with the approver), executing, refused (with the reason), stop requested |
| `[STOP]` | Each execution stopped, with the operator |
| `[AUTH]` | In JWT mode: chat, run or stop refused for a non-operator, or a session that belongs to someone else |
| `[TOOL:<name>]` | Tool inputs, and for some tools a `RESULT:` line with counts |
| `[CVE_VERIFY]`, `[FORECAST]`, `[CVE_SCOPE]`, `[ROLLBACK_WINDOW]`, `[PROMOTION]`, `[CHANGE_CALENDAR]`, `[PLAN_VIEW]` | Verification, forecasts, CVE package lists, rollback windows, promotions, the calendar, plan-time alarm reads |
| `[SLA_WINDOW]`, `[SCOPE_VALIDATION]`, `[FLEET_SUMMARY]`, `[CROSS_ACCOUNT]` | Window checks, scope checks, fleet reads, role assumptions |
| `[USAGE]` | Tokens, model calls, tool calls and timing for the turn |
| `PATCH_EXECUTED:` | Each patch run started, with the operator |

A `[USAGE]` line looks like this:

```
[USAGE] {"input_tokens": 41210, "output_tokens": 612, "cache_read_tokens": 36864, "cache_write_tokens": 0, "model_calls": 3,
         "tools": {"resolve_execution_scope": 1, "multi_account_execute": 1}, "tool_errors": 0,
         "stop_reason": "end_turn", "elapsed_ms": 14820, "first_text_ms": 9310}
```

Stop requests, and turns refused before the model runs, have none.

Tool lines don't carry the session ID, so match a conversation by time, or use the vended logs, which do. A plan's whole story is in the bucket: `plans/`, `plan-decisions/`, `plan-runs/` and `plan-executions/` under its ID (see [Security: audit trail](security.md#audit-trail)).

Logs Insights queries for the agent's log group:

```
# Plans, claims, refusals and stops
fields @timestamp, @message | filter @message like /\[PLAN\]|\[STOP\]/ | sort @timestamp desc | limit 50

# One tool
fields @timestamp, @message | filter @message like /TOOL:multi_account_execute/ | sort @timestamp desc | limit 20

# Token use per turn
fields @timestamp, @message | filter @message like /\[USAGE\]/ | sort @timestamp desc | limit 50
```

The UI API logs `[DASHBOARD]`, `[RECONCILE]`, `[PLAN]` (approvals) and `[AUTH]`. The report Lambda logs `[REPORT] <execution id> <status>: reconciled <n>` for each run event, and `[REPORT] sweep: reconciled <n>` every 5 minutes.

### Debug logs and traces

`PATCHY_DEBUG_LOGS=true` and `./deploy.sh agent` log the Strands and AgentCore Memory SDKs at DEBUG. That covers full prompts, tool inputs and outputs, and memory contents, so turn it off when you're done.

`ENABLE_TRACING=true` turns on CloudWatch Transaction Search and the runtime's trace delivery, which the GenAI Observability console reads. Spans take about 10 minutes to appear. The agent doesn't install the OpenTelemetry distribution, so traces show the runtime's invocations, not each tool call.

### Inspecting a run in SSM

```bash
# Recent Patchy runs in the hub
aws ssm describe-automation-executions \
  --filters Key=DocumentNamePrefix,Values=Patchy- --max-results 10

# Each account and Region of one run
aws ssm describe-automation-step-executions --automation-execution-id <execution-id>
```
