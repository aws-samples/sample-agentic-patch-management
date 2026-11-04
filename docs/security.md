# Security and audit

What protects a Patchy deployment, what it records for audit, and what to add before relying on it. Facts here are taken from the CDK stacks (`infra/lib/`), the UI API (`ui/api/server.py`) and the agent (`agent/`); where this page and the code differ, the code is right and this page is a bug.

- [Threat model](#threat-model)
- [Sign-in and roles](#sign-in-and-roles)
- [The approval chain](#the-approval-chain)
- [IAM by principal](#iam-by-principal)
- [Records in the compliance bucket](#records-in-the-compliance-bucket)
- [Audit trail](#audit-trail)
- [Data protection](#data-protection)
- [Container and supply chain](#container-and-supply-chain)
- [Before production](#before-production)

## Threat model

Patchy changes production instances, so the design assumes any one component can be wrong, including the model. A change needs three records written by two different principals: a plan written by the runtime, a decision written by the UI, and a one-time claim written by the runtime. IAM stops each principal writing the other's record.

| Actor | Could try | What stops it |
|---|---|---|
| The model, steered by a prompt injection in a finding, tag or tool result | Approve its own plan, run arbitrary commands, point an install at a hostile repository, stop someone else's run, leak data through the browser | Write tools only store plans. The runtime role is denied `plan-decisions/` and `baseline-overrides/` and can't delete anything. It starts only `Patchy-*` runbooks, whose parameters are pattern-checked, and there is no shell document. The model has no stop tool. Chat markdown renders no images and the CSP allows no remote origin. |
| An operator | Change a plan after review, run it twice, act on another conversation's plan, patch out of scope | The approval carries the plan hash shown in the change record. The claim is create-only, within 10 minutes of approval and before the plan's expiry. Plans, decisions and runs are bound to the conversation. Scope checks refuse out-of-scope targets, and the spoke role sends commands only to instances with the scope tag. Rollout limits come from the environment policy. |
| An approver | Approve their own request | `REQUIRE_SEPARATE_APPROVER=true` refuses it. That needs Cognito, since API keys and `DEFAULT_ROLE` give everyone one identity. |
| A viewer | Chat, approve, cancel or stop | The UI API returns 403. With `RUNTIME_AUTH=jwt` the runtime refuses too, from the user's own token. |
| A compromised UI container | Invent a plan, change what installs, fake a verified stage, rewrite a report | Its role is denied every plan, run, install-list, promotion and baseline record, and may only create a report, not replace one. It can't start or stop Automation, and assumes only the read-only spoke role. It can still approve or cancel a live plan in a conversation it knows, under any approver name, because writing decisions is its job. It can also delete pending report contexts, which suppresses those reports. `RUNTIME_AUTH=jwt` limits runs to requests carrying a signed-in operator's token. |
| A compromised runtime | Start a runbook without approval | IAM doesn't prevent this. The runtime role holds `ssm:StartAutomationExecution` on `Patchy-*` runbooks, so code running as the runtime can start one without a decision record. The runbooks' parameter patterns and the spoke role's scope tag condition still apply. Enforcing two-party approval in IAM would need a separate executor, which this sample doesn't have. |
| Another principal in the hub account | Invoke the runtime directly and ask it to run an approved plan | In IAM mode, anything allowed to invoke the runtime is trusted like the UI. `RUNTIME_AUTH=jwt` makes AgentCore reject any call without a valid Cognito token from the UI's app client. |
| A compromised spoke account | Read hub data, assume hub roles, mislead the agent | Spokes can read only the two override prefixes, and only from inside the organisation. No hub role trusts a spoke. The hub's role assumptions require the target role to be in `AWS_ORG_ID`. Spoke data is untrusted input to the model, and any change it prompts still lists its target accounts in the change record for an operator to read. |
| A cross-site page | Make a signed-in browser chat, approve or stop, or frame the UI | Every POST must be JSON from the UI's own origin. Responses forbid framing. |

Out of scope: an administrator of the hub account, a compromised Cognito user pool or identity provider, and a malicious package in a repository the patch baseline already trusts.

## Sign-in and roles

The UI API decides each request's role in one of three exclusive modes, tried in this order.

| Mode | When | How the role is decided |
|---|---|---|
| Cognito | `COGNITO_ENABLED` (default) with a certificate | The internet-facing load balancer signs users in. The API verifies both tokens on every request; membership of `operators` makes an operator, anything else a viewer. Membership of `auditors` adds a read-only list of every operator's plans. |
| API key | `COGNITO_ENABLED=false` with `API_KEY_OPERATOR`, `API_KEY_VIEWER` or `API_KEY` | The browser asks for the key, keeps it for the tab and sends it as `X-API-Key`. Keys are compared in constant time. They live in the Secrets Manager secret `patchy/ui-api-keys`, not in the image or the task definition. |
| None | Neither of the above | Every request gets `DEFAULT_ROLE`, `viewer` unless it's `operator` (case-insensitive). |

With Cognito off, the load balancer is internal and only a bastion (a `t3.nano` with IMDSv2 and `AmazonSSMManagedInstanceCore` only) can reach it.

Operators can chat, approve and cancel plans, and stop runs their conversation started. Viewers can read Overview, Findings and Reports. Auditors, in either role, also see every operator's plans from the last seven days (`GET /api/changes`). That list carries each plan's headline, requester, decision and runs, but not its conversation or stored parameters, so an auditor can't open, approve or cancel another operator's plan. Approval stays with the conversation that made the plan, because the runtime claims a plan only for the session its approval names. In API-key and open modes nobody is an auditor.

### Token verification

When Cognito is configured, the API verifies everything it uses about the caller:

- The load balancer's `x-amzn-oidc-data` token is verified as ES256 against the Region's ALB key. Its `signer` must be this load balancer and its `client` the Patchy app client. The email, or else the subject, is the user's identity.
- The `x-amzn-oidc-accesstoken` token, which carries the groups, is verified as RS256 against the user pool's keys, with these claims:
  - the issuer is the pool;
  - `token_use` is `access`;
  - the client is the Patchy app client;
  - the subject matches the identity token.
- The identity token is required on every request. A missing or invalid identity token, or an invalid access token, returns 401 and never falls back to an API key or `DEFAULT_ROLE`. A request without an access token is a viewer.

`/api/health` and `/signed-out` are the only paths the load balancer forwards without sign-in.

Chat, approve and stop are limited to 20 requests a minute per user, and forced dashboard refreshes to 4.

A POST must be `application/json` (415 otherwise). When the browser sends `Sec-Fetch-Site` it must be `same-origin` or `none`, and when it sends `Origin` the host must match. A cross-site form or fetch is refused with 403.

Every response, including errors, carries:

| Header | Value |
|---|---|
| `Content-Security-Policy` | `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'` |
| `X-Frame-Options` | `DENY` |
| `X-Content-Type-Options` | `nosniff` |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` |
| `Referrer-Policy` | `no-referrer` |

The UI serves its own fonts and scripts, so it loads nothing from another origin. Sign-out returns to `PUBLIC_URL`, never to the request's `Host` header.

### Cognito and TLS

| Setting | Value |
|---|---|
| Sign-up | Off; administrators create users |
| Passwords | At least 12 characters, with an upper-case letter, a lower-case letter and a digit |
| MFA | `COGNITO_MFA`: off, optional or required, by authenticator app. `required` only on a new pool. |
| Session | 12 hours at the load balancer. The browser also signs out after 60 minutes without activity. |
| TLS | Port 443 with `ELBSecurityPolicy-TLS13-1-2-Res-2021-06`; port 80 redirects. A self-signed certificate is refused on the public load balancer unless `ALLOW_SELF_SIGNED_CERT=true`. |

For production, use an [ACM public certificate](https://docs.aws.amazon.com/acm/latest/userguide/acm-public-certificates.html) for your own domain and set `PUBLIC_URL`. To use an existing identity provider, [federate the user pool](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-identity-federation.html) and map its groups to `operators`, `viewers` and `auditors`.

### Runtime authorisation

By default (`RUNTIME_AUTH=iam`) the UI calls the runtime with its task role, and the runtime trusts the UI's role check. A request that carries a bearer token in IAM mode is refused for changes, because it means an authoriser is configured that Patchy doesn't know about.

With `RUNTIME_AUTH=jwt`, the deploy gives the runtime a JWT authoriser whose issuer is the UI's user pool and whose only allowed client is the UI's app client. AgentCore then rejects every call without a valid token from that client, SigV4 included. The UI forwards the signed-in user's access token, and the runtime takes the user and their groups from it, never from the request body (`agent/helper/caller.py`):

- Chat turns, `run_plan`, `stop_run` and promotion records need membership of `operators`.
- Plans record the token's user as the requester.
- A conversation belongs to its first user: the runtime creates `session-owners/<session id>.json` once and refuses anyone else on that session.

The deploy reads the pool from `Patchy-UI`, so deploy once in IAM mode, then set `RUNTIME_AUTH=jwt` and redeploy. In JWT mode, scripts that invoke the runtime with AWS credentials are refused.

## The approval chain

| Step | Record | Written by | Protected by |
|---|---|---|---|
| Plan | `plans/<id>.json`: the tool, the exact parameters (with rollout limits and the stop alarm), session, requester, hash, 15-minute expiry, and the change record | Runtime, create-only | UI and Lambda denied. The hash is SHA-256 over the tool, parameters, session and creation time, and is checked again by both the UI and the runtime. |
| Decision | `plan-decisions/<id>.json`: an approval (hash, approver, time) or a cancellation (who, time) | UI API, create-only, so the first decision wins | Runtime and Lambda denied. The API requires an operator, the same conversation, a matching hash and an unexpired plan. |
| Claim | `plan-runs/<id>.json`: claim time and approver | Runtime, create-only, so a plan runs once | Made only for an operator's approval of this hash and session, before expiry, less than 10 minutes old. The Change Calendar and the CVE install list are checked again first. |
| Run | `run-executions/<execution id>.json` per location (plan, session, approver, start time), then `plan-executions/<plan id>.json` listing every location | Runtime, create-only, at the moment each run starts | The UI shows and stops only runs whose record names its conversation. Each start carries a `ClientToken` derived from the plan and location, so a retry can't double-run. |
| Report context | `pending-reports/<execution id>.json` | Runtime, create-only | UI and Lambda may only delete it, after writing the report. |
| Report | `YYYY/MM/DD/<execution id>.json` | Report Lambda or the UI's reconciler, create-only | Runtime denied. Neither writer can replace a report. |

If the same approver presses Approve again for a plan whose run never started, the UI API sends the run again; the runtime still claims the plan only once. The approver of record for the run is the decision's approver.

## IAM by principal

### Runtime role (`PatchyAgentCorePolicy`, `Patchy-Core`)

Allows:

- reading EC2, SSM (including Change Calendar state and maintenance window tasks), Inspector, CloudWatch, CloudWatch Logs and Organizations;
- `ssm:CreateMaintenanceWindow` and `ssm:AddTagsToResource`, only with the tag `ManagedBy=IntelligentPatchAutomation`. Registering a target or task and deleting a window apply only to windows with that tag;
- `ssm:StartAutomationExecution` on `Patchy-*` documents, and reading and stopping executions;
- `iam:PassRole` on `PatchySpokeRole`, to SSM only;
- assuming `PatchySpokeRole` in accounts of the organisation, in multi-account mode;
- reading, writing and listing the compliance bucket.

Denies:

| Statement | Effect |
|---|---|
| `NoPlanApprovals` | No writes or deletes under `plan-decisions/` |
| `CreateOnlyRecords` | `plans/`, `plan-runs/`, `run-executions/`, `plan-executions/`, `session-owners/`, `pending-reports/` and `install-overrides/` only with `If-None-Match`, so nothing is overwritten |
| `NoReportWrites` | No writes to dated reports |
| `NoBaselineOverrideWrites` | No writes under `baseline-overrides/` |
| `NoDeletes` | No object or version deletes anywhere in the bucket |

The runtime has no `ssm:SendCommand`, no `ssm:CancelCommand` and no patch baseline writes. It can overwrite `promotions/` records.

### UI task role (`Patchy-UI`)

Allows:

- reading EC2, SSM (including run status), Inspector and the bucket;
- creating `plan-decisions/` records and dated reports;
- deleting `pending-reports/` records;
- invoking only the Patchy runtime (the policy is a deny-all placeholder if the deploy can't find the runtime ARN);
- reading AgentCore Memory events;
- assuming `PatchySpokeReadRole`.

Denies writes and deletes on:

- `plans/`, `plan-runs/`, `run-executions/` and `plan-executions/`;
- `install-overrides/`, `promotions/`, `baseline-overrides/` and `session-owners/`;
- `pending-reports/`, which it may only delete.

`plan-decisions/` and the dated reports are create-only. The role can't start or stop Automation.

### Report Lambda role (`Patchy-UI`)

Allows:

- reading SSM runs, command results and patch states;
- creating dated reports, and deleting `pending-reports/` records;
- publishing to the run topic, with KMS when `NOTIFY_KMS=true`;
- assuming `PatchySpokeReadRole`.

Denies writes to every plan, decision, run, install-list, promotion, baseline and session record. Its log group keeps 30 days.

The Lambda runs on two EventBridge rules: a Patchy run reaching a final state, and every 5 minutes. Each rule retries twice.

### Spoke roles (`Patchy-SpokeIam`)

`PatchySpokeRole` trusts only:

- the hub runtime role's exact ARN;
- `ssm.amazonaws.com`, with `aws:SourceAccount` set to the hub or the spoke.

It allows:

- starting `Patchy-*` Automation;
- sending `AWS-RunPatchBaseline` and `Patchy-*` commands to instances carrying the scope tag;
- reading EC2, SSM, Inspector and CloudWatch;
- `cloudwatch:DescribeAlarms` and stopping its own account's executions, which SSM needs for the stop alarm;
- reading the hub's two override prefixes.

`PatchySpokeReadRole` trusts only the exact ARNs of the UI task role and the report Lambda role, and reads EC2, SSM, Inspector and CloudWatch.

Both cap sessions at one hour. Before the trusted roles exist, on a first deploy, each falls back to a role name pattern in the hub and the deploy warns. A full deploy replaces the pattern with exact ARNs once the UI is up, in one account or many. A later warning in the deploy output means the pattern is still in place; run `./deploy.sh spoke`.

### Bucket policy

The bucket policy has two statements:

- It denies any request without TLS.
- It allows `s3:GetObject` on `baseline-overrides/` and `install-overrides/`, so instances can read their override files. The readers are principals in the organisation (`aws:PrincipalOrgID`), or in the hub account when `AWS_ORG_ID` isn't set. That is enough for instances in the hub account. An instance in another account also needs `s3:GetObject` on both prefixes in its own role, because a cross-account read needs both accounts to allow it (see [Deploying: before you start](deploying.md#before-you-start)).

The bucket is versioned, encrypted with S3-managed keys, blocks public access, and expires objects after 365 days.

## Records in the compliance bucket

| Prefix | Holds | Runtime | UI | Report Lambda |
|---|---|---|---|---|
| `plans/` | Plans | create | denied | denied |
| `plan-decisions/` | Decisions | denied | create | denied |
| `plan-runs/` | Claims | create | denied | denied |
| `run-executions/` | One record per started run | create | denied | denied |
| `plan-executions/` | Every run a plan started | create | denied | denied |
| `session-owners/` | Conversation owners (JWT mode) | create | denied | denied |
| `pending-reports/` | Context for reports not yet written | create | delete | delete |
| `install-overrides/` | CVE package lists | create | denied | denied |
| `promotions/` | Verified environments | write | denied | denied |
| `run-locks/` | The start lock: which plan is starting | write, conditional | denied | denied |
| `baseline-overrides/` | Severity override files | denied | denied | denied |
| `YYYY/MM/DD/` | Compliance reports | denied | create | create |

"Create" means create-only: a write with `If-None-Match`, which fails if the object exists. `baseline-overrides/` is written only from the deploy machine. The runtime writes `run-locks/` only with `If-None-Match` or `If-Match`, although IAM doesn't require that. The UI and report Lambda roles are denied the prefix, so neither can hold the lock to stop plans starting.

## Audit trail

### Who did what

| Question | Where to look |
|---|---|
| Who asked for the change? | `plans/<id>.json`, `requested_by` |
| Who approved it, and when? | `plan-decisions/<id>.json`, `approver` and `decided_at` |
| When did it start? | `plan-runs/<id>.json`, `claimed_at`; `run-executions/<execution id>.json`, `started_at` |
| What ran where? | `plan-executions/<plan id>.json`, then each execution in SSM Automation history |
| What was the outcome? | The report `YYYY/MM/DD/<execution id>.json`, and the Reports view |
| Which AWS calls were made? | CloudTrail, in the hub and each spoke |

The approver is the operator of record for the run. It appears in:

- the report's `operator` field and its S3 metadata;
- the runtime log lines `[PLAN] claimed <plan> approver=...` and `PATCH_EXECUTED: operator=...`;
- the role session name of the runtime's own calls into spokes, `patchy-<operator>-<account>`.

Automation steps in spokes run as SSM sessions of `PatchySpokeRole`. The dashboard and the Lambda appear as `patchy-dashboard-<account>`.

Without Cognito, the approver is recorded as `operator@<first 12 characters of the session ID>`, so runs can't be tied to a person. Use Cognito where that matters.

### Correlating a run

A report's `report_id` is the SSM execution ID, which links three sources:

- S3 records: `run-executions/<id>.json` gives the plan ID, and from there `plans/`, `plan-decisions/`, `plan-runs/` and `plan-executions/`. Patch reports also carry `execution.plan_id` directly.
- CloudTrail: `StartAutomationExecution` returns the execution ID in `responseElements.automationExecutionId`. Its `clientToken` is derived from the plan ID.
- SSM: the execution's history lists each account and Region.

### Report contents

A patch report has these fields:

| Field | Contents |
|---|---|
| Top level | `report_id`, `timestamp` (the execution's start in SSM), `operation_type`, `operator`, `reconciled_at`, `install_packages` |
| `execution` | `execution_id`, `plan_id`, `status`, `decision`, `duration_seconds`, `failure_message`, `success_count`, `failure_count`, and `incomplete` and `unread_locations` when some outcomes couldn't be read |
| `vulnerability` | `cve_id`, `severity`, `cvss_score`, `additional_cve_ids` |
| `scope` | `environment`, `targeting`, `instance_ids`, `instance_count`, `account_ids`, `regions`, `severity_filter`, `tags` (each `REPORT_TAGS` tag's value, `mixed` or `unknown`), `tag_values` (the values of a `mixed` tag) |
| `compliance` | `sla_hours`, `sla_source`, `first_observed_at`, `sla_met`, `sla_reason`, `frameworks` |
| `patch_state` | `pre_patch`, `post_patch` |
| `reboot` | `option`, `pending_instance_ids` |

`sla_met` is true or false only when the run succeeded on every instance, patched at least one, and both the SLA hours and the finding's discovery time are known. Otherwise it is null and `sla_reason` says why. A report marked `incomplete` was written although some locations' outcomes couldn't be read (a spoke role that couldn't be assumed, or command results that couldn't be listed). Its counts leave those locations out, `unread_locations` names them, `sla_met` is null, and `scope.instance_ids` is the list recorded when the run started rather than the partial count. A rollback report has the top-level fields except `install_packages`, `execution` without `decision` or `plan_id`, and `scope` without `severity_filter`.

Each report also carries S3 object metadata for quick filtering: `cve-id`, `severity`, `environment`, `decision-type`, `sla-met`, `frameworks`, `operator`, `reboot-pending`, and `tag-<name>` for each report tag, with the name in lower case and other characters as hyphens. `sla-met` is left out when the result is unknown.

### Queries

The approval chain for one plan:

```bash
B=patch-compliance-reports-<account-id>
for p in plans plan-decisions plan-runs plan-executions; do
  aws s3 cp "s3://$B/$p/<plan-id>.json" /dev/stdout 2>/dev/null; echo
done
```

Check that the decision's `plan_hash` equals the plan's and its `approver_role` is `operator`. `claimed_at` should be within 10 minutes of `approved_at` and before the plan's `expires_at`.

Approvals, runs and stops, in CloudWatch Logs Insights on the runtime log group:

```
fields @timestamp, @message
| filter @message like /\[PLAN\] claimed|PATCH_EXECUTED|\[STOP\] STOPPING|\[PLAN\] refused|\[AUTH\]/
| sort @timestamp desc
| limit 200
```

To query the reports with Athena, point a Glue crawler at the bucket and exclude every record prefix in the table above, so only the dated reports are catalogued. Confirmed SLA breaches in prod:

```sql
SELECT report_id, "timestamp", operator, vulnerability.cve_id, compliance.sla_hours, compliance.sla_reason
FROM patch_compliance_reports
WHERE scope.environment = 'prod' AND compliance.sla_met = false
ORDER BY "timestamp" DESC;
```

Runs started and stopped, in CloudTrail Lake:

```sql
SELECT eventTime, eventName, userIdentity.arn, element_at(requestParameters, 'documentName') AS document
FROM <event-data-store-id>
WHERE eventName IN ('StartAutomationExecution', 'StopAutomationExecution')
ORDER BY eventTime DESC;
```

`StopAutomationExecution` comes from the runtime role when an operator presses Stop. In a spoke, it comes from `PatchySpokeRole` when a stop alarm fires.

## Data protection

| Data | Where | Kept |
|---|---|---|
| Plans, decisions, run records, reports | Compliance bucket | 365 days; versioned |
| Conversations | AgentCore Memory | 30 days |
| UI log | `/patch-automation/ui` | 14 days |
| Report Lambda log | Lambda log group | 30 days |
| Runtime log | `/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT` | Not set by Patchy; set a retention policy |
| Vended runtime logs (`ENABLE_RUNTIME_LOGS`) | `/aws/vendedlogs/bedrock-agentcore/...` | 14 days |
| VPC flow logs | Flow log group | 2 years |

The bucket and the SNS topic refuse requests without TLS. With `NOTIFY_KMS=true`, the topic and the UI and Lambda log groups use a customer managed key, rotated yearly; the runtime's log groups don't.

The UI logs the first 80 characters of each message, escaped onto one line, and the runtime logs the first 80 as they are. `PATCHY_DEBUG_LOGS=true` logs full prompts, tool inputs and outputs, and memory contents. Leave it off except while diagnosing.

`./deploy.sh destroy` deletes the bucket and everything in it. To keep records after teardown, set the bucket's removal policy to `RETAIN` and remove `autoDeleteObjects` in `infra/lib/core-stack.ts`.

## Container and supply chain

| Control | Where |
|---|---|
| Base image pinned by digest (`python:3.12-slim` from ECR Public) | `Dockerfile` |
| Python dependencies installed with `--require-hashes` | `ui/api/requirements.lock` |
| Runs as a non-root user | `Dockerfile` |
| Read-only root filesystem; `/tmp` is a task volume | `infra/lib/ui-stack.ts` |
| Fonts and scripts bundled into the image; nothing loaded from a CDN | `ui/frontend` |
| Tasks in private subnets without public IPs; only the load balancer reaches port 8000 | `infra/lib/ui-stack.ts` |

## Before production

This is a sample. Before relying on it:

- Keep Cognito on, with MFA (`COGNITO_MFA=required` on a new pool), federated to your identity provider.
- Set `RUNTIME_AUTH=jwt`, so the runtime checks each caller itself, and `REQUIRE_SEPARATE_APPROVER=true` where two-person control is required.
- Use a trusted certificate on your own domain, and consider AWS WAF in front of the load balancer.
- Run a full `./deploy.sh` and confirm there are no trust fallback warnings.
- Set a retention policy on the runtime log group, and decide whether reports must outlive the stack (removal policy) and 365 days (lifecycle).
- For records nobody in the hub can alter, replicate the bucket to a separate audit account. Or turn on S3 Object Lock, and check that its retention fits the bucket's 365-day lifecycle and removal policy.
- Add alarms on agent errors, run failures, UI 5xx responses and unhealthy tasks.
- Run more than one UI task (`desiredCount` in `infra/lib/ui-stack.ts`). Rate limits and caches are per task.
- Treat the remaining gaps as known risks: a compromised runtime can start a Patchy runbook without approval, and a compromised UI can approve a live plan.
