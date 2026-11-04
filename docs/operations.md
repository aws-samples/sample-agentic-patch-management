# Operations

How to use Patchy day to day: asking, reviewing and approving a change, following and stopping a run, and reading findings and reports. It also covers the tags Patchy works from and how to manage users. To deploy or configure Patchy, see [Deploying](deploying.md).

- [The screen](#the-screen)
- [Asking](#asking)
- [Reviewing and approving a change](#reviewing-and-approving-a-change)
- [Following and stopping a run](#following-and-stopping-a-run)
- [After a run](#after-a-run)
- [Findings and reports](#findings-and-reports)
- [Tags and SLAs](#tags-and-slas)
- [Users and roles](#users-and-roles)
- [Bedrock Guardrails](#bedrock-guardrails)

## The screen

The chat sits on the left and the detail pane on the right. The rail down the left edge chooses what the detail pane shows:

- Overview: instances, critical findings, patch compliance and SLA results; what needs attention; each environment, with its critical and high findings and its patch compliance as a percentage with the count ("80% (4 of 5)"); recent changes. Each environment row has **Instances**, **Scan…**, which puts a dry-run scan request in the chat box for you to send, and **SLA risk**, which asks which findings will miss their deadline.
- Changes: plans and runs from this conversation, one row per change (a plan reads as its run once approved), and every Patchy run in progress across all operators, including a stopped run whose commands are still finishing. Auditors also see every operator's plans from the last seven days.
- Findings: every open Inspector finding on in-scope instances.
- Reports: every patch and rollback run of the last 30 days.

A plan or a run opens in the detail pane as soon as it appears in the chat, and the chat keeps a link back to it. The browser's Back and Forward buttons move between the panes you opened, and a reload reopens the last one. Times always show the date. **New conversation** asks you to confirm before it starts a new one, and while an earlier conversation loads, the chat says so. **Open in new window** on a change record or run shows it in a window of its own (see [Pop-out windows](#pop-out-windows)).

The top bar holds the account picker, the theme switch and your role. With more than one account, the picker narrows Overview and Findings to one account; Reports and the SLA figure always cover every account. The theme follows your operating system until you choose one, and the browser remembers the choice. Your role comes from your Cognito group and can't be changed here.

For operators, Cmd+K or Ctrl+K opens a list of things to ask. With Cognito sign-in, Patchy signs you out after 60 minutes without activity in any of its windows.

## Asking

Type in the chat and press Enter. Name the environment ("in dev"), the CVE or severity, and account IDs or instance IDs when you mean specific ones. The agent works best with requests like these:

| To | Ask |
|---|---|
| See the fleet | `Show instances in dev` |
| See findings | `What critical vulnerabilities do we have in prod?` |
| See where a CVE is | `How widespread is CVE-2026-40553?` |
| Check compliance | `Show patch compliance status for staging` |
| See SLA risk | `Which findings in prod will miss their SLA?` |
| Preview | `Preview patches in prod` (a dry-run scan; installs nothing) |
| Patch a CVE | `Patch CVE-2026-40553 in dev` |
| Patch by severity | `Patch HIGH and above in staging` |
| Patch instances | `Patch i-0abc1234567890def in dev` |
| Patch now despite a window | `Patch CVE-2026-40553 in prod now, don't wait for the window` |
| Roll back | `Roll back the last dev patch run` or `Roll back i-0abc1234567890def` |
| Check a run | `Check status of 0f8b2a1c-...` |
| Check a fix | `Did CVE-2026-40553 get fixed on i-0abc1234567890def?` |
| Check before promoting | `Verify health` |
| Create a window | `Create a maintenance window for staging, Sundays at 3 am for 3 hours` |
| Look back | `Show SLA breaches this week` or `Who patched prod last week?` |

Buttons in the detail pane, on cards in the chat, and in the Cmd+K list follow one rule. If the prompt only reads, it sends straight away. If it could change something, it fills the chat box for you to check and send.

A dry-run scan starts an SSM scan as soon as you ask, without a plan. It installs nothing. The reply names the scan and offers **Show scan results**, which asks for them once the scan has had time to finish. Patchy creates maintenance windows in the hub account and Region only.

## Reviewing and approving a change

A patch, rollback or maintenance window request never runs straight away. Patchy makes its checks, stores the exact parameters as a plan, and opens the plan's **change record**:

- The headline and status say what the change is and how long the plan has left. A plan expires 15 minutes after it's made.
- The summary says what installs, where and when, in one sentence.
- Checks lists what Patchy checked before making the plan:

  | Check | Means |
  |---|---|
  | In scope | Every target carries the scope tag in a configured account and Region |
  | Change calendar open, or Change calendar | The calendar is open now, or none is configured. [`CHANGE_CALENDAR`](deploying.md#scope-and-change-controls) says what happens to a run when it closes |
  | Only the CVE's packages, or Patch level | What installs |
  | Window and SLA | Why it runs now: no maintenance window or patch policy patches every target before the SLA deadline, you asked to patch now, or it's a severity plan |
  | dev verified, or staging verified | Every environment except dev: a run in the previous environment with the same patch scope (the CVE, the severity and higher, or all approved patches) finished in the last 14 days and was verified healthy. The check gives the date the run finished. The 14 days count from when the run finished, not from when it was verified. |
  | Dry-run scan first, or No dry-run for this plan | Whether a dry-run scan ran for this plan. It's optional, so not running one is a note, not a warning. Without one, the check says when Systems Manager last scanned the targets, as a range when their latest scans are more than an hour apart. With one, Patchy checks the scan when it makes the plan, and again when the plan runs: it refuses the plan if the scan didn't succeed, is more than two hours old, or wasn't a Patchy scan of these targets (another environment, account or Region, or none of the named instances). A scan that covered only some of the named instances, or an environment scan used for a plan by instance ID, shows as a warning instead, since Patchy can't confirm it covered every target |
  | Stop alarm present, or Stop alarm | Whether the stop alarm exists in every target account and Region and isn't already firing, or that none is configured |
  | Not in the fleet view yet | Shown only when some targets aren't in SSM Explorer yet. Patchy read them from EC2, so it has their tags but no patch state; the run includes them, and **Where** marks each one as pending sync |

  A tick means the check passed, an exclamation mark is a warning you should read, and a dash is information.
- What changes, Where, When, How it rolls out, Reboot, Stops if, Undo describe the run. When a schedule covers some of a CVE plan's targets, **When** adds a coverage table by account and Region. **Where** shows a grid of accounts and Regions and lists every target instance with its name, account and Region (the first 500, with the total).

**Approve and run** records your approval and starts the plan once, with exactly the parameters shown. If anything about the request changes, ask again: Patchy makes a new plan. **Cancel plan** throws the plan away. Typing "yes" or "approve" in the chat does nothing.

An approval is refused when:

- the plan has expired;
- someone else already approved or cancelled it;
- the plan changed after the record was shown;
- `REQUIRE_SEPARATE_APPROVER=true` and you asked for the plan yourself.

Viewers see the record but no approve button.

If a maintenance window or Quick Setup patch policy will patch every target before the CVE's SLA deadline, Patchy says so and makes no plan. Ask to patch now if you don't want to wait. If a schedule covers only some targets, the plan patches all of them now, and the change record's **When** section shows which schedule covers which targets (see [SLA](#sla)). A plan by severity always runs now, because each finding it covers has its own deadline.

### Pop-out windows

**Open in new window** shows a change record or run in its own window, which is useful on a second screen or in a change review call. The pop-out stays in step with the main window: approving, cancelling or stopping there is carried out by the main window, which owns the conversation, and both windows show the result. If the main window is closed, the pop-out acts on its own, with the same checks.

## Following and stopping a run

Once a plan starts, the run view opens. It shows:

- progress, counted by location, since SSM reports progress per account and Region;
- the status in each account and Region;
- a timeline of what has happened.

It refreshes every 10 seconds while the run is active. If the status can't be read, the view keeps the last status it had, says "Status not updated", and keeps trying, less often each time. It stops refreshing once every location has ended and no patch command the run sent is still going.

If a location didn't start, the run view says which one and why. The plan's approval is spent by then, so make a new plan for the locations that didn't start.

**Stop run** asks for confirmation, then cancels the run in every account and Region the plan started. Only an operator can stop a run, and only from the conversation that started it. The agent can't stop runs, so asking it to stop does nothing.

A stop doesn't halt everything at once:

- Locations not yet started don't start.
- In a location already started, Run Command may still reach the instances it hasn't yet, and an instance already being patched finishes, and stays patched.
- SSM marks the run Cancelled straight away, so a location can read as stopped while its instances are still patching. While they are, for up to three hours after the stop, the run view says "Stopped; instances already patching are finishing" and keeps refreshing, and the run stays under "Running now" in Changes until they finish.

### Stop alarms

`ABORT_ALARMS` names one CloudWatch alarm, because SSM watches one alarm per run. To watch several metrics, combine them in a [composite alarm](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Create_Composite_Alarm.html) and name that. Each location of a run polls the alarm in its own account and Region:

- When the alarm goes into ALARM mid-run, SSM cancels the instances not yet started. One already being patched finishes, even though its step may show as Cancelled.
- A location whose alarm is already in ALARM doesn't start.
- A location without the alarm stops at the start, which is why the change record checks every location first.

A plan keeps the alarm it was made with, so a change to `ABORT_ALARMS` applies only to new plans.

## After a run

### Report

When a run ends, the report Lambda writes its compliance report, usually within a minute and at most about 5 minutes later. It appears under Reports, and with `NOTIFY_TOPIC=true` the outcome also goes to the SNS topic.

Two cases wait longer. For a stopped run, the report waits until the patch commands the run had already sent have finished, so it counts those instances. When some locations' outcomes can't be read, the report waits for them. Either way, after three hours the report is written as it stands; one with unread locations is marked incomplete, names them, and has no SLA result.

### Verify

`Verify health` checks that every location of the plan has finished, that the instances are online in SSM, and that their CloudWatch alarms are quiet. A healthy result records the environment as verified for that CVE or severity, which the next environment's plan checks.

- While a location is still running, it says to wait.
- Alarms are given time to settle from when the last location finished: 2 minutes, or longer when an instance's alarm takes longer to evaluate (its period times its evaluation periods, plus a minute), up to 30 minutes.
- A failed run, or instances SSM reports unhealthy, gives a rollback recommendation.
- It holds, without recommending a rollback, when:
  - an operator stopped the run, since stopping is the operator's decision and not a failure;
  - the run partly failed, or another location of the plan failed or can't be read;
  - alarms are firing, or an instance alarm went to INSUFFICIENT_DATA after the run started (the metric may have stopped because of the run);
  - health couldn't be read, for example when Systems Manager can't be reached in one account. Unread health is never a reason to roll back.

### Check a fix

`Did CVE-X get fixed on i-...?` asks Inspector per instance. When a finding is still open, it says why: a reboot is pending, Inspector hasn't rescanned yet, or the fixed version isn't installed. For a CVE in several packages, the fix counts as installed only when every package is at its fixed version. An instance EC2 reports terminated isn't counted; one Patchy can't find where it looked reads "Couldn't locate" rather than terminated.

### Promote

Patch the next environment as a new request, for example dev, then staging, then prod. Each environment is its own plan and approval.

### Roll back

A rollback undoes the yum or dnf transactions that began during the last finished Patchy patch run on the targets in the last 30 days, including a failed or stopped run. Because the window is time-based, another package change made on an instance during that run's window is undone too. Instances with no transactions in the window are left as they are. The change record names the run it undoes and how that run ended, for example "ended Failed after 28 s": the last run can be one that changed nothing, such as a run that failed at the start, and then the rollback changes nothing either.

Patchy refuses a rollback when there is no such run, and while a newer patch run on the same targets has no report yet, for example a run you just stopped, because the last reported run would then be the wrong one to undo.

Asked whether a rollback worked, Patchy checks the run and, when it has the patch state captured around the patch run, scans the instances and compares patch counts. It confirms the rollback only when every location succeeded, the comparison ran and shows the patches reverted on every instance, every instance is online in SSM, and no CloudWatch alarms are firing. Otherwise it lists what it couldn't confirm, for example a scan that failed or health it couldn't read, and recommends a look. Without that patch state, it says only that the rollback run succeeded and the instances are healthy, and that it can't confirm the patches were reverted.

## Findings and reports

**Findings** lists each open Inspector finding with its severity, CVSS score and whether a patch fixes it (fully, or partly when Inspector says a fix covers only some of the affected packages), and where it is. Filter by severity or to fixable findings, search by ID or title, and export the filtered list as CSV. Operators get **Plan patch** on findings a patch fixes, which fills the chat box with a request for that CVE in the lowest environment it affects (dev first, prod last), and **Instances**, which asks for the instances with their account and Region.

**Reports** lists every patch and rollback run of the last 30 days, with:

- its result (patched, failed or stopped, with counts);
- its SLA result: met, breached, or no result when the run didn't succeed on every instance, the finding had no discovery time, or the run patched by severity (its findings have different deadlines, so there is no single result);
- when it ran.

Filter to patches, rollbacks or breached SLAs, and by environment. **Details** on a row shows:

- the CVE and the packages installed;
- why the run went when it did. For a run that went ahead at once, the reason the plan found: you asked to patch now, it was a severity plan, no SLA applied, the schedules couldn't be checked, no maintenance window or patch policy patches the targets before the SLA deadline, or some targets aren't covered by one. Reports written before this was recorded say only that no window met the SLA or you asked to patch now;
- the SLA hours and where they came from, and for a met or breached SLA the arithmetic: the deadline (first seen plus the SLA hours) against when the run finished, and how early or late that was;
- the compliance frameworks;
- the [report tags](#report-tags);
- the outcome counts and any failure message;
- the approver, the plan ID, and the run's SSM Automation execution ID in full, to copy.

A run ID in **Recent changes** on Overview opens Reports filtered to that run, with its details open.

Export writes the filtered runs as CSV.

## Tags and SLAs

Patchy works from EC2 instance tags.

| Tag | Values | Effect |
|---|---|---|
| `PatchAutomation` | `enabled` | In scope. Without it Patchy can't see, scan or patch the instance. Change the tag with `SSM_SCOPE_TAG_KEY` and `SSM_SCOPE_TAG_VALUE`. |
| `Environment` | `dev`, `staging`, `prod` | The environment's rollout limits, reboot policy and promotion order. |
| `SLA-CRITICAL`, `SLA-HIGH`, `SLA-MEDIUM`, `SLA-LOW` | Hours, as a whole number | The SLA for findings of that severity. Defaults: 24, 72, 168 and 720. |
| `ComplianceFrameworks` | For example `PCI-DSS,SOC2` | Recorded in reports as audit context. It doesn't change any decision. |
| `Team`, `Product`, or the tags in `REPORT_TAGS` | Any | Recorded in reports. See [Report tags](#report-tags). |

### Environment

Patchy accepts common spellings and treats them as one environment:

- `development` and `non prod` count as dev;
- `stage` and `uat` count as staging;
- `production` counts as prod.

`test`, `testing` and `qa` form a test environment, which gets prod's limits. So does any other value.

Rollout limits for each environment:

| Environment | Locations (account and Region) at a time | Stop after failed locations | Instances at a time in each | Stop after failed instances |
|---|---|---|---|---|
| dev | 100% | 25% | 50% | 25% |
| staging | 50% | 1 | 25% | 1 |
| prod | 1 | 0 | 10% | 0 |

The tag drives some further rules:

- An instance without an `Environment` tag counts as prod: it gets prod's limits and reboot policy. A plan that names it under any other environment is refused. Environment-wide runs match the tag, so they don't reach untagged instances.
- A request can't file an instance under another environment. A plan naming a prod instance "in dev" is refused.
- If instances in one environment carry two spellings, such as `prod` and `Production`, an environment-wide plan is refused until they're retagged to one value. A run matches one value.
- Outside dev, a plan by instance ID can't span more than one account and Region. Patch by environment instead, or make one plan per location.

### SLA

For each finding, the SLA is the instance's tag for that severity, or the default. Across several instances the strictest applies. The deadline runs from when Inspector first observed the finding on the plan's own targets, in the configured scope; a sighting on some other instance doesn't start this plan's clock.

When Patchy can't read what the SLA depends on, it doesn't wait for a window:

- If some instances' tags can't be read, their SLA could be stricter than anything read, so the plan patches now and says the tags couldn't be read.
- If Inspector can't be read for the first-observed time, Patchy can't show any schedule meets the deadline, so the plan patches now and says why.
- If Inspector can't be read for the CVE's severity, Patchy makes no plan, because the SLA would otherwise rest on the agent's guess.

Before planning a run, Patchy checks whether something already scheduled would patch each target in time. Two kinds of schedule count: a maintenance window in the instance's own account and Region, and a Quick Setup patch policy the hub can read, which means the hub's own policies and organisation policies (a delegated administrator can read organisation policies but only the management account can create them). Policies created locally in a spoke account aren't visible from the hub and don't count. A schedule covers an instance only when Patchy can show all of the following:

- It installs: a window with an `AWS-RunPatchBaseline` task whose operation is Install, or a policy set to scan and install. A policy must have deployed successfully, with no failed or unfinished async executions.
- It targets the instance. For a policy, that means the instance's account or an OU above it (nested OUs count), its Region, and either all instances or the policy's tag.
- Its next run finishes before the deadline. Windows use their duration; a policy's run is given two hours.
- It reboots if the fix needs one. A schedule that installs without rebooting covers the instance only when the plan wouldn't reboot either, or when the CVE's packages are known and none is a kernel package.
- For a CVE, the patch baseline approves the fix: the instance's last patch scan lists a missing patch for that CVE. Above 100 instances this isn't checked, and the plan runs now.

If every target is covered, Patchy recommends waiting and names the schedules. Otherwise the plan patches every target now, and the change record shows a coverage table by account and Region: the instances there, the schedule that covers them, whether it finishes before the deadline, and why the rest aren't covered. Cancelling that plan means waiting, and the uncovered instances breach their SLA.

A window counts when its targets match the instance, however it targets it (an `Environment` tag, a Patch Group tag or instance IDs). Windows and policies are read at most every five minutes.

Anything Patchy can't read or prove leaves the instance uncovered, for example a policy it can't open, an OU lookup that fails or a cron expression it can't evaluate. The plan names any policy, or any account and Region's maintenance windows, that it couldn't read. A delegated administrator can be refused some organisation policies (`UnauthorizedException` on `GetConfigurationManager`) while it reads others.

Patchy looks for policies in the hub's Region and in each Region in `SPOKE_REGIONS`, because a policy is listed only in the Region it was created in. A policy created elsewhere isn't seen, so its instances are patched now. Patching only the uncovered instances isn't offered in this release: outside dev a plan by instance ID can't span more than one account and Region.

### Report tags

Each report records the instance tags named in `REPORT_TAGS`, `Team,Product` by default, as they were when the run started. Set it to the tags your organisation uses to assign ownership, such as `REPORT_TAGS=CostCentre,Owner,Application`, and run `./deploy.sh agent`. Reports written before the change keep the tags they recorded.

- When every instance in a run has the same value, the report records it. When they differ, it records `mixed` and the list of values. An instance without the tag shows as not tagged.
- **Details** shows each tag, the CSV export has a column for each, and the SLA summary above the list is grouped by the first tag.
- The tags don't change any decision.

### When tag changes take effect

SLA and `Environment` tags are read from EC2 on each request, so a change takes effect on the next one. The scope check and the list of instances in an environment use the agent's fleet view from SSM Explorer, which it refreshes every five minutes; an instance not in that view is checked against EC2 directly. So Patchy can still plan for an instance up to five minutes after its scope tag is removed.

When the plan starts, each targeted instance's scope tag is checked in EC2 again, so it isn't patched once the tag is gone, and the spoke role's permission refuses it too.

Plans for an environment and the Overview dashboard also ask EC2 for scope-tagged instances, so a newly launched instance counts straight away, in plans, change records and Overview. A run targets the `Environment` tag, so it patches those instances too. The fleet answers list such an instance as pending sync, with no patch state, until Explorer ingests it, which can take minutes or hours.

## Users and roles

| Role | Cognito group | Can |
|---|---|---|
| Operator | `operators` | Everything: chat, approve and cancel plans, stop runs |
| Viewer | `viewers` | Overview, Findings and Reports. No chat, no plans, no changes. |
| Auditor | `auditors`, alongside either role | Also a read-only list in **Changes** of every operator's plans from the last seven days: the change, who asked, whether it was approved, cancelled or left to expire and by whom, and the run it started, which links to its report. An auditor can't open another operator's conversation, approve or cancel. |

A user in neither group is a viewer.

To add a user, run `./deploy.sh create-user`. It asks for the email address, a temporary password, the role (operator unless you type `viewer`) and whether the user is also an auditor, and adds the user to the groups without sending an email. The user sets a new password at first sign-in. Passwords need at least 12 characters, with an upper-case letter, a lower-case letter and a digit.

To script it instead:

```bash
POOL_ID=$(aws cloudformation describe-stacks --stack-name Patchy-UI \
  --query 'Stacks[0].Outputs[?OutputKey==`CognitoUserPoolId`].OutputValue' --output text)
aws cognito-idp admin-create-user --user-pool-id "$POOL_ID" --username user@example.com \
  --user-attributes Name=email,Value=user@example.com Name=email_verified,Value=true \
  --message-action SUPPRESS --temporary-password '<temporary-password>'
aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL_ID" \
  --username user@example.com --group-name operators
```

A password on the command line is visible to other users of the machine; `create-user` avoids that. Remove a user with `aws cognito-idp admin-delete-user`.

Some points to watch:

- MFA. `COGNITO_MFA=optional` lets users add an authenticator app, and `required` makes it mandatory. `required` can only be set when the pool is created.
- Federation. To use an existing identity provider (Okta, Microsoft Entra ID, IAM Identity Center), federate the user pool through SAML or OIDC and map the provider's groups to `operators`, `viewers` and `auditors`.
- Replacing the pool. A change that replaces the user pool deletes every user, and so does setting `COGNITO_ENABLED=false`. Check the change set before deploying changes to `Patchy-UI`.

## Bedrock Guardrails

`agent/setup_guardrail.py` creates a sample Bedrock guardrail. It turns away questions unrelated to patching, filters harmful content, blocks prompt-injection attempts, and masks AWS account IDs and IP addresses in replies.

```bash
venv/bin/python agent/setup_guardrail.py   # the venv exists after a first deploy
```

Put the `GUARDRAIL_ID` and `GUARDRAIL_VERSION` it prints into `.env` and run `./deploy.sh agent`. The runtime applies the guardrail to the latest message in each turn. Adjust its policies in the Bedrock console to suit your requirements.
