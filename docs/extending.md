# Extending Patchy

How to add a tool, a card, a change type or an Automation document, how to support another OS or scanner, and how to call Patchy from other systems. Read [Architecture](architecture.md) first: most extensions plug into shared code for routing, approval and audit, and that page explains it.

- [Adding a read tool](#adding-a-read-tool)
- [Showing a result as a card](#showing-a-result-as-a-card)
- [Adding a change type](#adding-a-change-type)
- [Adding an Automation document](#adding-an-automation-document)
- [Other operating systems](#other-operating-systems)
- [Other vulnerability scanners](#other-vulnerability-scanners)
- [Calling Patchy from other systems](#calling-patchy-from-other-systems)
- [Seeing everyone's changes](#seeing-everyones-changes)

## Adding a read tool

A read tool looks something up and returns a dict. It needs no approval.

1. Write the function in the module under `agent/helper/tools/` that matches what it reads: `vulnerability_tools.py`, `fleet_tools.py`, `maintenance_tools.py`, `compliance_tools.py` or `patch_tools.py`.

   ```python
   from typing import Optional
   from strands import tool
   from ._shared import logger, classify_error, ClientError

   @tool
   def get_reboot_status(environment: str, instance_ids: Optional[list] = None) -> dict:
       """Which instances in an environment are waiting for a reboot after patching.

       Use for "what needs a reboot in prod". For whether a CVE is fixed, use verify_cve_remediation.

       Args:
           environment: dev, staging or prod.
           instance_ids: Limit to these instances.
       """
       try:
           logger.info(f"[TOOL:get_reboot_status] env={environment}")
           ...
           return {"environment": environment, "pending": pending, "count": len(pending)}
       except ClientError as e:
           return classify_error(e)
   ```

2. Export it from `agent/helper/tools/__init__.py` (the import and `__all__`).
3. Import it in `agent/supervisor.py` and add it to `ALL_TOOLS`, the only tool list the agent is built with.
4. Add a progress label in `ui/frontend/src/lib/toolLabels.ts`, such as "Checking reboots". Without one, the UI turns the tool name into words.
5. Add a routing scenario to the `unified` list in `agent/eval/scenarios.json`, run the eval, and update the baseline. See [the eval](../agent/eval/README.md).
6. Add the tool to the table in [Architecture](architecture.md#tools) and update the tool count there and in the README.

The docstring is the tool's description, and the model reads it on every call to decide which tool fits. Say what the tool returns and when to use it, and name the alternative where two tools overlap. Keep implementation notes in code comments, so they don't cost tokens on every call.

To steer what the model does next, return a `next_action` string, for example "Now call verify_cve_remediation with these instance IDs". Leave rules that must always hold to code, as steering (`agent/helper/steering.py`) or as checks in the tool.

## Showing a result as a card

If the operator should see a result as data rather than prose, return a `display` block with it:

```python
return {
    "pending": pending,
    "display": {
        "type": "reboots",
        "title": f"Reboots pending · {environment}",
        "subtitle": f"{len(pending)} instances",
        "fields": [{"label": "Oldest", "value": oldest, "tone": "warn"}],
        "rows": [{"label": i["id"], "status": i["since"], "tone": "warn"} for i in pending],
        "actions": [{"label": "Reboot dev", "prompt": "Patch dev and reboot"}],
    },
}
```

`CardCollector` (`agent/helper/cards.py`) takes the block out before the model or memory sees it, sends it to the UI, and tells the model to reply in two or three sentences without repeating the card. A card larger than 24,000 bytes is dropped, with a `[CARD]` warning in the log.

The chat renders cards in `ui/frontend/src/components/cards/ResultCards.tsx`:

- `fleet` and `vulnerabilities` have their own components. Their builders are in `agent/helper/display.py`; `vulnerabilities` also links to the Findings view.
- `plan` and `run` appear as links that open the change record and the run view in the detail pane (`ui/frontend/src/views/`). The change record reads the plan's stored view; see [Adding a change type](#adding-a-change-type).
- Every other type is a generic card. It shows `title`, `subtitle`, `fields` (label, value, optional tone), `rows` (label, status, optional tone) and `actions`. The tones are `ok`, `warn` and `bad`.

An action is `{"label", "prompt"}`. Clicking it sends the prompt at once if it only reads, and otherwise fills the chat box for the operator to send (`isReadOnlyPrompt` in `ui/frontend/src/lib/actions.ts`). A prompt counts as a read when it starts with a verb such as show, list, check or verify, and contains no change word such as patch, install, roll back, reboot or stop.

## Adding a change type

Anything that changes infrastructure goes through a plan. A write tool never acts when the model calls it: it checks, stores a plan and returns it, and the runtime runs the stored parameters after an operator approves.

1. Write an `_impl` function in the domain module that takes `confirm_execute`:
   - With `False`, it runs every check and returns `{"status": "confirmation_required", "execution_plan": {...}}` without changing anything. Any other result, such as a refusal, goes straight back to the model.
   - With `True`, it makes the change. Start Automation through the start helpers in `agent/helper/tools/_shared.py`, so the run gets the plan's stored limits, a `ClientToken`, a per-location stop alarm, and a run record the moment it starts. Return `automation_execution_id` or `automation_execution_ids`.
2. Add a `@tool` wrapper in `agent/helper/tools/plan_tools.py` that calls `_plan_or_pass("<tool name>", <impl>, params)`. Its docstring should say it returns a plan for approval and never executes.
3. Add an `EXECUTORS` entry in the same file. `run_approved_plan` refuses a plan whose tool has none.
4. Choose the shared checks the tool needs. Add it to the tuples `_PATCH_TOOLS`, `_ROLLBACK_TOOLS`, `_ENVIRONMENT_TOOLS` and `_INSTANCE_TOOLS` as they apply. Add it to the list that stores the rollout limits and stop alarm in the plan (`_store_controls`).
5. Describe the change for the operator:
   - Add a branch in `_card` (the chat link's text and fallback fields).
   - Add a branch in `agent/helper/plan_view.build`. It returns the change record: headline, summary, checks, sections, and the targets and locations. Report only checks the code actually made; anything else is an `info` row.
6. Register the tool as for a read tool, with a label such as "Preparing a reboot plan". If it targets instance IDs or an environment, add it to the sets in `agent/helper/steering.py`.
7. Grant the AWS actions to the runtime role (`PatchyAgentCorePolicy` in `infra/lib/core-stack.ts`) and, for spoke accounts, to `PatchySpokeRole` (`infra/lib/spoke-iam-stack.ts`). Never grant them to the UI task role, the report Lambda or the read role.

The approval, the claim, the run records, Stop and the report then work without further code. [Security](security.md#the-approval-chain) lists the records involved.

## Adding an Automation document

Patchy's documents are defined in `infra/lib/patchy-ssm-docs-stack.ts`. To add one:

1. Add an `ssm.CfnDocument` named `Patchy-<name>` with `updateMethod: 'NewVersion'`. Give every parameter an allowed pattern from `PATTERNS` or a fixed list of values, and take `AutomationAssumeRole`.
2. If it runs a Command document, add that document too, and a dependency on it.
3. Add the name as a constant in `agent/helper/tools/_shared.py`.
4. Run `./deploy.sh docs`.

The runtime and spoke roles allow `Patchy-*` Automation and commands, and the report rule matches the `Patchy-` prefix, so a document named that way needs no IAM change. A document that sends a command other than `AWS-RunPatchBaseline` or `Patchy-*` does need one.

## Other operating systems

Patching works on any OS that Patch Manager supports, because it runs `AWS-RunPatchBaseline`. Three parts are OS-specific:

| Part | Today | To add an OS |
|---|---|---|
| Severity scope | `BaselineOverride` files for Amazon Linux 2 and 2023, RHEL, CentOS, Rocky, Alma, Oracle Linux, SUSE and Windows (`OS_FILTERS` in `agent/setup_baseline_overrides.py`) | Add an `OS_FILTERS` entry. Ubuntu and Debian rank by priority, not severity, so they need a priority mapping rather than a severity filter. |
| CVE scope | Linux only; Windows targets are refused | On Windows, `InstallOverrideList` applies only to patches the baseline already approves, so it can't scope one CVE |
| Rollback | `Patchy-YumUndo`, for the yum and dnf family | A Command document per package manager, and runbooks that pick it by platform (the fleet cache records each instance's platform) |

## Other vulnerability scanners

To show findings from Qualys, Tenable or Rapid7, add a read tool that calls the scanner's API. Return a `display` block from `vulnerability_card()` in `agent/helper/display.py` to get the same card as Inspector findings. It reads `severity_counts`, and `findings` with `cve_id`, `severity`, `cvss_score`, `fix_available` and `affected_resources`.

Inspector stays the source for everything that acts on findings: CVE-scoped plans, verification, forecasts, SLA discovery times and the Findings view. Driving plans from another scanner means changing `agent/helper/cve_scope.py` and `agent/helper/verification.py` as well.

Container image findings from Amazon ECR are available by setting `INSPECTOR_RESOURCE_TYPES=EC2,ECR`. Patchy reports them, but patching a container means rebuilding the image, which Patchy doesn't do.

## Calling Patchy from other systems

The UI API streams the agent's replies as server-sent events. A caller must:

- send `Content-Type: application/json`;
- pass the cross-site check, with no foreign `Origin`;
- authenticate. With API keys configured, send `X-API-Key`. With Cognito, the load balancer requires a browser sign-in, so a bot needs its own route to the API.

| Endpoint | Use |
|---|---|
| `POST /api/chat` with `message` and `session_id` | Ask a question or request a plan. Read the `text`, `card` and `done` events. |
| `GET /api/plans/<id>?session_id=` | A plan's change record and status, for the conversation that made it |
| `GET /api/runs/<id>?session_id=` | A run's status by account and Region |
| `GET /api/dashboard` | Fleet, findings, runs and compliance as JSON |

Some integrations:

- Run outcomes reach Slack or Teams through the SNS topic (`NOTIFY_TOPIC=true`) and AWS Chatbot.
- A ServiceNow or Jira flow can ask for impact or a plan, then link the ticket to the plan's change record.
- Grafana or an internal portal can read `/api/dashboard`.

Approval always happens in the UI, by an operator, in the conversation that made the plan.

## Seeing everyone's changes

A plan belongs to the conversation that made it. The plans API returns a plan only to that conversation, and **Changes** lists only its own plans and runs, so one operator can't open, approve or cancel another's plan. Three views cover the whole team:

- **Changes** has a "Running now, all operators" table with every Patchy run in progress.
- **Reports** lists every patch and rollback run of the last 30 days from every operator. Each row names who approved it, and the search box finds runs by approver, run ID, plan ID or CVE. The CSV export carries the same columns.
- Users in the `auditors` Cognito group also get a read-only list in **Changes** of every operator's plans from the last seven days, including plans still waiting, cancelled or expired (`GET /api/changes` in `ui/api/server.py`).

For a longer history than seven days, the records are in the compliance bucket: `plans/<plan_id>.json` holds the plan and its change record, `plan-decisions/<plan_id>.json` the approval or cancellation, and `plan-executions/<plan_id>.json` the runs it started. Query them with S3 or Amazon Athena, or raise `ALL_CHANGES_DAYS` in `ui/api/server.py`. Whatever you add, keep it read-only: approval and cancellation must stay with the conversation that made the plan, because the runtime claims a plan only for the session the approval names.
