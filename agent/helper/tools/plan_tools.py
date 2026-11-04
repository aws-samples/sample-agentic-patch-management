"""Write tools that produce approvable plans instead of executing.

Each tool runs the same validation and gates as before, then stores the exact
parameters as a plan (helper.plans) and returns it with a plan card. Nothing
runs until an operator approves the plan in the UI; the runtime then executes
it through EXECUTORS with the stored parameters.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from strands import tool

from helper import change_controls, cve_scope, plan_view, plans, rollback_window, run_guard
from ._shared import (AWS_REGION, SPOKE_REGIONS, _normalize_environment, execution_limits, get_operator,
                      get_operator_message, get_session_id, reboot_option, set_operator)
from .maintenance_tools import _create_maintenance_window_impl
from .patch_tools import (
    _scan_check,
    _execute_patch_operation_impl,
    _multi_account_execute_impl,
    _multi_account_rollback_impl,
    _rollback_patches_impl,
)

logger = logging.getLogger(__name__)

_NO_SCAN_WARNING = "No dry-run scan was run for this plan."

_PLAN_NEXT_ACTION = (
    "Nothing has run. The plan is shown to the operator as a card with an Approve "
    "button. In two or three sentences, say what the plan will do and anything they "
    "should check. Do not ask them to type yes; approval only happens on the card. "
    "Don't add a Next Steps section; the card has the buttons."
)


def _installs_text(severity_filter: Optional[str], cve_id: Optional[str]) -> str:
    if severity_filter:
        scope = f"Approved {severity_filter.upper()} and higher patches"
    else:
        scope = "All patches the patch baseline approves"
    if cve_id:
        scope += f" (requested for {cve_id}; other patches at that level install too)"
    return scope


def _shorten(ids: List[str], n: int = 3) -> str:
    shown = ", ".join(ids[:n])
    return shown + (f" and {len(ids) - n} more" if len(ids) > n else "")


def _window_card(record: dict, params: dict, execution_plan: dict) -> dict:
    env = execution_plan.get("environment", "")
    limits = execution_limits(env or "prod")
    return {
        "type": "plan", "plan_id": record["plan_id"], "plan_hash": record["plan_hash"],
        "status": "awaiting_approval", "expires_at": record["expires_at"],
        "title": f"Maintenance window plan · {env}",
        "subtitle": f"Plan {record['plan_id'][:6]} · expires in {plans.PLAN_TTL_MIN} min · nothing has run yet",
        "fields": [
            {"label": "Name", "value": params["name"]},
            {"label": "Schedule", "value": params["schedule"]},
            {"label": "Duration", "value": f"{params['duration']} h"},
            {"label": "Targets", "value": f"Scoped instances tagged Environment={env}"},
            {"label": "Where", "value": f"This account, {AWS_REGION} only"},
            {"label": "Patching", "value": "A patch task installs approved patches (AWS-RunPatchBaseline) "
                                           "each time the window opens.", "tone": "warn"},
            {"label": "Rollout", "value": (f"instances at once: {limits['send_command_max_concurrency']} "
                                           f"(stop after {limits['send_command_max_errors']} failed)")},
            _reboot_field(params.get("reboot_option") or "NoReboot", f"{env} policy", env),
        ],
        "decision": None, "warnings": [],
    }


def _card_warnings(params: dict, execution_plan: dict, is_rollback: bool, rollback_run: Optional[dict]) -> List[str]:
    warnings = []
    untagged = execution_plan.get("untagged_environment_instances") or []
    if untagged:
        warnings.append(f"No Environment tag on {_shorten(untagged)}; counted as prod.")
    if not params.get("scan_execution_id") and not is_rollback:
        warnings.append(_NO_SCAN_WARNING)
    if is_rollback and rollback_run:
        warnings.append("The window is time-based: a yum change another tool made on these instances during "
                        "that run is undone too. Instances the run didn't change are left alone.")
    return warnings


def _card(tool_name: str, record: dict, params: dict, execution_plan: dict,
          reboot_source: str = "", rollback_run: Optional[dict] = None) -> dict:
    """Build the plan card shown to the operator."""
    if tool_name == "create_maintenance_window":
        return _window_card(record, params, execution_plan)
    env = execution_plan.get("environment") or params.get("environment") or ""
    is_rollback = "rollback" in tool_name
    fields: List[Dict[str, str]] = []
    if tool_name in _INSTANCE_TOOLS:
        ids = list(execution_plan.get("instance_ids") or params.get("instance_ids") or [])
        fields.append({"label": "Targets", "value": f"{len(ids)} instance(s): {_shorten(ids)}"})
    else:
        accounts = list(execution_plan.get("accounts") or params.get("account_ids") or [])
        regions = list(execution_plan.get("regions") or params.get("regions") or SPOKE_REGIONS)
        fields.append({"label": "Targets", "value": f"Every scoped instance tagged Environment={env}"})
        # The operator approves exactly these accounts, so show them, not just a count.
        shown = ", ".join(accounts[:10]) + (f" and {len(accounts) - 10} more" if len(accounts) > 10 else "")
        fields.append({"label": "Where", "value": f"{len(accounts)} account(s): {shown} × {', '.join(regions)}"})
    if is_rollback and rollback_run:
        when = datetime.fromisoformat(rollback_run["started_at"]).astimezone(timezone.utc).strftime("%d %b %H:%M UTC")
        patched = list(rollback_run.get("instance_ids") or [])
        if patched:
            fields.append({"label": "Patched by that run",
                           "value": f"{len(patched)} instance(s): {_shorten(patched)}"})
        # How that run ended: the newest run may be one that changed nothing
        # (disruption round, 2 Nov: a run that failed on the patch lock after 28 s).
        outcome = rollback_window.outcome_text(rollback_run)
        fields.append({"label": "Action", "value": f"Undo the yum transactions that began during patch run "
                                                   f"{rollback_run['execution_id'][:8]} ({when}{outcome}) on each instance",
                       "tone": "warn"})
    elif params.get("install_packages"):
        pkgs = params["install_packages"]
        shown = ", ".join(pkgs[:8]) + (f" and {len(pkgs) - 8} more" if len(pkgs) > 8 else "")
        fields.append({"label": "Installs", "value": f"{shown} (latest available, for {params.get('cve_id')})"})
    else:
        fields.append({"label": "Installs", "value": _installs_text(
            execution_plan.get("severity_filter") or params.get("severity_filter"), params.get("cve_id"))})
    limits = execution_limits(env or "prod")
    if tool_name in _ENVIRONMENT_TOOLS:
        accounts_at_once, account_errors = params.get("max_concurrency"), params.get("max_errors")
        instances_at_once, instance_errors = limits["send_command_max_concurrency"], limits["send_command_max_errors"]
    else:
        accounts_at_once, account_errors = limits["account_max_concurrency"], limits["account_max_errors"]
        instances_at_once, instance_errors = limits["instance_max_concurrency"], limits["instance_max_errors"]
    fields.append({"label": "Rollout", "value": (
        f"Accounts at once: {accounts_at_once} (stop after {account_errors} failed) · "
        f"instances at once: {instances_at_once} (stop after {instance_errors} failed)")})
    if not is_rollback:
        fields.append(_reboot_field(params.get("reboot_option") or "NoReboot", reboot_source, env))
    alarms = change_controls.abort_alarms()
    if alarms and tool_name != "create_maintenance_window":
        fields.append({"label": "Stops if", "value": ", ".join(alarms) + " goes into ALARM in any target account and Region (each needs the alarm, or its run stops)"})
    decision = None
    if execution_plan.get("decision") == "EMERGENCY":
        sla = execution_plan.get("sla_hours")
        why = execution_plan.get("why_now")
        decision = {"tone": "warn", "text": (
            f"Runs now. SLA {sla} h ({execution_plan.get('sla_source') or 'default'}); "
            + (f"{why}." if why else "no maintenance window covers these instances before the deadline, "
               "or you asked to patch now.")
            if sla else (f"Runs now: {why}." if why else "Runs now."))}
    warnings = _card_warnings(params, execution_plan, is_rollback, rollback_run)
    verb = "Rollback" if is_rollback else "Patch"
    if is_rollback:
        level = "undo the last patch run"
    elif params.get("severity_filter"):
        level = f"{params['severity_filter'].upper()} and higher"
    elif params.get("cve_id"):
        level = params["cve_id"]
    else:
        level = "all approved patches"
    return {
        "type": "plan",
        "plan_id": record["plan_id"],
        "plan_hash": record["plan_hash"],
        "status": "awaiting_approval",
        "expires_at": record["expires_at"],
        "title": f"{verb} plan · {env or 'instances'} · {level}",
        "subtitle": f"Plan {record['plan_id'][:6]} · expires in {plans.PLAN_TTL_MIN} min · nothing has run yet",
        "fields": fields,
        "decision": decision,
        "warnings": warnings,
    }


def _view(tool_name: str, params: dict, execution_plan: dict, reboot_source: str, rollback_run: Optional[dict],
          promotion_missing: Optional[str], coverage_text: str,
          promotion_ended: Optional[datetime] = None) -> Optional[dict]:
    """The plan's change record for the UI; a plan without one still shows its card."""
    try:
        view = plan_view.build(tool_name, params, execution_plan, reboot_source=reboot_source,
                               rollback_run=rollback_run, promotion_missing=promotion_missing,
                               promotion_ended=promotion_ended)
    except Exception as e:
        logger.warning(f"[PLAN] change record for {tool_name} could not be built: {e}")
        return None
    if coverage_text:
        for section in view["sections"]:
            if section["key"] == "what":
                section["text"] = section["text"].rstrip(".") + coverage_text + "."
    return view


def _with_policy_limits(params: dict) -> Tuple[Optional[dict], List[str]]:
    """Set the account limits from the environment's policy; a request can't choose its own.

    The policy is defined once per environment (cross_account.execution_limits),
    the plan stores it and the card shows it, so the executor runs what was approved.
    """
    env = _normalize_environment(params.get("environment") or "") or "prod"
    policy = execution_limits(env)
    params["max_concurrency"] = policy["account_max_concurrency"]
    params["max_errors"] = policy["account_max_errors"]
    return None, []


def _store_controls(params: dict, environment: Optional[str] = None) -> None:
    """Keep the rollout limits and abort alarms in the plan, so the hash covers what the run uses."""
    env = _normalize_environment(environment or params.get("environment") or "") or "prod"
    params["rollout"] = execution_limits(env)
    params["abort_alarms"] = change_controls.abort_alarms()


def _plan_targets(params: dict) -> List[str]:
    """Instance IDs a patch plan targets: the named ones, or the environment's scoped instances."""
    from .patch_tools import _environment_instance_ids
    if params.get("instance_ids"):
        return list(params["instance_ids"])
    return _environment_instance_ids(_normalize_environment(params.get("environment") or ""),
                                     params.get("account_ids") or [])


def _target_platforms(instance_ids: List[str]) -> set:
    """Platforms of these instances, from the fleet cache, or EC2 for ones Explorer doesn't list yet
    (2 Nov review, L5: a new Windows instance otherwise passed the Linux-only CVE check)."""
    from . import _shared
    _shared._get_fleet_summary()
    cache = _shared.fleet_instances()
    out = {cache[i].get("platform") for i in instance_ids if i in cache and cache[i].get("platform")}
    missing = [i for i in instance_ids if i not in cache]
    if missing:
        out |= {v.get("platform") for v in _shared._direct_describe_scoped_instances(instance_ids=missing).values()
                if v.get("platform")}
    return out


def _run_scope(params: dict) -> dict:
    """What a patch or rollback plan would touch, for the check against runs in progress."""
    if params.get("instance_ids"):
        return {"instance_ids": list(params["instance_ids"])}
    return {"environment": params.get("environment"), "account_ids": list(params.get("account_ids") or []),
            "regions": list(params.get("regions") or [])}


def _unreported_refusal(params: dict) -> Optional[dict]:
    """A rollback refuses while a patch run covering its targets has no report yet: the last
    reported run would be the wrong one to undo (2 Nov review, T1)."""
    try:
        runs = run_guard.unreported_runs(_run_scope(params))
    except Exception as e:
        logger.warning(f"[PLAN] couldn't list unreported runs: {e}")
        return _refusal("ReportPendingUnknown",
                        "Patchy couldn't check for patch runs whose reports aren't written yet, so it can't be "
                        "sure which run is the last one to undo.", "Say so, and suggest trying again in a few minutes.")
    if not runs:
        return None
    run = runs[0]
    return _refusal("ReportPending",
                    f"Patch run {str(run.get('execution_id'))[:8]} covers these targets and its report isn't "
                    "written yet, so the last reported run is not the one to undo.",
                    "Say so: wait for that run's report (Reports shows it, usually within 5 minutes of the "
                    "run's commands finishing), then ask again.", pending_execution_id=run.get("execution_id"))


def _busy_refusal(params: dict) -> Optional[dict]:
    run = run_guard.blocking_run(_run_scope(params))
    if not run:
        return None
    return _refusal("RunInProgress", f"{run_guard.describe(run)}, and it covers instances this change targets.",
                    "Say so: wait for that run to finish (the Changes view shows it), then ask again.",
                    running_execution_id=run.get("execution_id"))


# Plans made in this conversation, by what they would run: asking twice for the same
# change shows the plan already waiting instead of a second one to approve. Kept in
# process (a runtime session stays on one instance); a miss just makes a new plan.
_session_plans: Dict[Tuple[str, str], str] = {}
_SESSION_PLANS_MAX = 500
# A waiting plan with less time than this left is replaced, so there's time to review it.
_REUSE_MIN_SECONDS = 180


def _plan_key(tool_name: str, params: dict) -> Tuple[str, str]:
    return get_session_id(), plans.plan_hash(tool_name, params, "", "")


def _waiting_plan(tool_name: str, params: dict) -> Optional[dict]:
    """The same plan from this conversation, still waiting for a decision, or None."""
    plan_id = _session_plans.get(_plan_key(tool_name, params))
    if not plan_id:
        return None
    try:
        plan = plans.load_plan(plan_id)
        expires = datetime.fromisoformat(plan["expires_at"])
        if (plan["session_id"] != get_session_id() or plans.get_record(f"plan-decisions/{plan_id}.json")
                or plans.get_record(f"plan-runs/{plan_id}.json")
                or (expires - datetime.now(timezone.utc)).total_seconds() < _REUSE_MIN_SECONDS):
            return None
    except Exception as e:
        logger.info(f"[PLAN] not reusing {plan_id}: {e}")
        return None
    logger.info(f"[PLAN] {tool_name}: the same plan {plan_id} is still waiting; showing it again")
    return plan


def _remember_plan(tool_name: str, params: dict, record: dict) -> None:
    if len(_session_plans) >= _SESSION_PLANS_MAX:
        _session_plans.pop(next(iter(_session_plans)))
    _session_plans[_plan_key(tool_name, params)] = record["plan_id"]


def _refusal(code: str, message: str, next_action: str, **extra) -> dict:
    return {"error": message, "error_code": code, "result_type": "error",
            "next_action": f"Nothing was planned. {next_action}", **extra}


def _scope_to_cve(params: dict) -> Tuple[Optional[dict], List[str], Optional[Tuple[int, int]]]:
    """For a CVE patch without a severity filter, install only the CVE's packages.

    Adds install_packages and install_override_url to the plan's parameters, so
    the approval covers exactly what installs. A plan by instance ID keeps only
    the instances Inspector lists for the CVE. A plan by environment can't be
    narrowed per instance (one runbook per location, so the rollout limits
    hold): the listed packages install on every scoped instance there that has
    them, and the card says how many of those Inspector lists as affected.

    Returns:
        (refusal, warnings, coverage): a refusal result when the CVE can't be
        scoped that way, else None; warnings for the plan card; and for a plan
        by environment, (affected, targeted) instance counts.
    """
    if not params.get("cve_id") or params.get("severity_filter"):
        return None, [], None
    targets = _plan_targets(params)
    if not targets:
        return _refusal("NoTargets", "No scoped instances were found for this plan.",
                        "Say so; the fleet view may still be syncing."), [], None
    if any("windows" in (p or "").lower() for p in _target_platforms(targets)):
        return _refusal("CveScopeUnsupported",
                        "CVE-scoped installs work on Linux only; the targets include Windows instances.",
                        "Offer a severity-scoped patch instead (severity_filter)."), [], None
    found = cve_scope.cve_packages(params["cve_id"], instance_ids=targets)
    if found.get("unreadable"):
        return _refusal("CveScopeUnreadable",
                        f"Inspector couldn't be read everywhere for {params['cve_id']} ({found['unreadable'][0]}), so "
                        "the affected instances and the packages that fix them aren't fully known.",
                        "Say so, and suggest trying again in a few minutes."), [], None
    if found.get("truncated"):
        return _refusal("CveScopeTooLarge",
                        f"Inspector returned more findings for {params['cve_id']} than Patchy reads at once, so "
                        "the package list could be incomplete.",
                        "Say so, and offer a severity-scoped patch (severity_filter) or a narrower plan "
                        "by account or instance ID."), [], None
    if not found["packages"]:
        return _refusal("NoPackageFix",
                        f"Inspector lists no OS package fix for {params['cve_id']} on the {len(targets)} "
                        f"instance(s) this plan targets.",
                        "Say why, and offer a severity-scoped patch instead (severity_filter) if the "
                        "operator still wants to patch.", not_patchable=found["not_patchable"]), [], None
    params["install_packages"] = found["packages"]
    params["install_override_url"] = cve_scope.write_install_override(found["packages"])
    warnings = (["Not patched by Patch Manager: " + ", ".join(found["not_patchable"]) + "."]
                if found["not_patchable"] else [])
    coverage = None
    if params.get("instance_ids"):
        affected = set(found["instances"])
        unaffected = [i for i in params["instance_ids"] if i not in affected]
        if unaffected:
            params["instance_ids"] = [i for i in params["instance_ids"] if i in affected]
            warnings.append(f"Left out, not affected by {params['cve_id']}: {_shorten(unaffected)}.")
    else:
        coverage = (len(found["instances"]), len(targets))
    return None, warnings, coverage


# A reboot asked for as part of the change ("and reboot it", "reboot if needed",
# "with a reboot"), or refused ("don't reboot", "without rebooting"). Merely
# mentioning reboots ("is a reboot pending?") is neither.
_NO_REBOOT = re.compile(r"\b(no|don'?t|do\s+not|never|without|skip|avoid|not)\b[\w\s]{0,20}?\breboot", re.I)
_REBOOT = re.compile(r"\b(and|then|with|also|please)\s+(a\s+)?reboot\b"
                     r"|\breboot\s+(it|them|the\s+\w+|if\s+(needed|required|necessary)|afterwards|after)\b"
                     r"|^\s*reboot\b", re.I)


def reboot_requested_in(message: str) -> Optional[bool]:
    """Whether the operator's message asks for a reboot (True), refuses one (False), or neither (None)."""
    if _NO_REBOOT.search(message or ""):
        return False
    if _REBOOT.search(message or ""):
        return True
    return None


def _resolve_reboot(params: dict) -> str:
    """Turn the operator's reboot request into the plan's reboot_option; return where it came from.

    An explicit request wins: the model's argument, or else the operator's own words this
    turn (the model sometimes leaves a conditional "reboot if needed" out). Otherwise the
    environment's policy applies. The card shows the choice before anything is approved.
    """
    requested = params.pop("reboot", None)
    if requested is None:
        requested = reboot_requested_in(get_operator_message())
    env = _normalize_environment(params.get("environment") or "") or "prod"
    if requested is None:
        params["reboot_option"] = reboot_option(env)
        return f"{env} policy"
    params["reboot_option"] = "RebootIfNeeded" if requested else "NoReboot"
    return "requested"


def _reboot_field(option: str, source: str, env: str) -> dict:
    if option == "RebootIfNeeded":
        field = {"label": "Reboot", "value": f"Reboots if needed ({source}). Instances restart once the "
                                             "install finishes, so new kernels and libraries take effect."}
        if (env or "").lower() == "prod":
            field["tone"] = "warn"
        return field
    return {"label": "Reboot", "value": f"No reboot ({source}). Kernel and library fixes take effect at the "
                                        "next reboot; until then Inspector may still report them."}


def _promotion_check(params: dict) -> Tuple[Optional[dict], List[str], Optional[str], Optional[datetime]]:
    """(refusal, warnings, missing stage, ended): the previous stage wasn't verified for this
    scope, and when the previous stage's verified run ended, if there is one.

    A refusal only if staged rollout is required; otherwise a warning on the card.
    """
    env = _normalize_environment(params.get("environment") or "") or "prod"
    scope = change_controls.scope_key(params)
    missing, ended = change_controls.stage_check(env, scope)
    if not missing:
        return None, [], None, ended
    message = change_controls.promotion_text(missing, scope, True, ended)
    if change_controls.staged_rollout_required():
        return _refusal("StageNotVerified", message, f"Offer to patch {missing} first and verify it."), [], missing, ended
    return None, [message + f" Patch and verify {missing} first, or approve knowingly."], missing, ended


# Which checks apply to which write tool.
_PATCH_TOOLS = ("execute_patch_operation", "multi_account_execute")
_ROLLBACK_TOOLS = ("rollback_patches", "multi_account_rollback")
_ENVIRONMENT_TOOLS = ("multi_account_execute", "multi_account_rollback")  # account limits on the plan
_INSTANCE_TOOLS = ("execute_patch_operation", "rollback_patches")


def _scan_targets(tool_name: str, params: dict) -> dict:
    """What the plan's dry-run scan has to have covered: its instances, or its environment's locations."""
    if tool_name in _INSTANCE_TOOLS:
        return {"instance_ids": list(params.get("instance_ids") or [])}
    return {"environment": _normalize_environment(params.get("environment") or ""),
            "account_ids": list(params.get("account_ids") or []),
            "regions": list(params.get("regions") or SPOKE_REGIONS)}


def _plan_or_pass(tool_name: str, impl: Callable[..., dict], params: dict) -> dict:
    """Run the tool's gates; store a plan when it would execute, else pass the result through."""
    limit_warnings: List[str] = []
    if tool_name in _ENVIRONMENT_TOOLS:
        # The run targets every tagged instance, so the checks need the whole fleet.
        from . import _shared
        _shared._get_fleet_summary()
        if _shared.fleet_view_truncated():
            return _refusal("FleetViewTruncated",
                            f"The fleet view stopped at MAX_FLEET_SIZE ({_shared.MAX_FLEET_SIZE}) instances, so "
                            "this plan's checks can't see every instance the run would target.",
                            "Say so: raise MAX_FLEET_SIZE (and redeploy the agent), or plan by instance ID.")
        explorer_error = _shared.fleet_view_error()
        if explorer_error:
            return _refusal("FleetViewUnavailable",
                            f"SSM Explorer couldn't be read ({explorer_error}), so the fleet view this plan's checks "
                            "use may be out of date.",
                            "Say so: it usually clears on the next refresh; plan by instance ID meanwhile, which "
                            "checks each instance in EC2 directly.")
        unreadable = _shared.fleet_view_unreadable()
        if unreadable:
            return _refusal("FleetViewIncomplete",
                            f"Instances in {', '.join(unreadable[:5])} couldn't be read, so this plan's checks "
                            "can't see every instance the run would target there.",
                            "Say so. If the reason is throttling it clears when the fleet view refreshes "
                            "(every five minutes); if it's AccessDenied or UnauthorizedOperation, Patchy's role "
                            "can't read that account and Region, so check its permissions there.")
        refused, limit_warnings = _with_policy_limits(params)
        if refused:
            return refused
    reboot_source = ""
    rollback_run = None
    cve_coverage = None
    promotion_missing = promotion_ended = None
    freeze = change_controls.calendar_closed()
    if freeze:
        return _refusal("ChangeFreeze", freeze, "Say when changes are allowed again.")
    alarm_problem = change_controls.abort_alarms_problem()
    if alarm_problem and tool_name != "create_maintenance_window":
        return _refusal("AbortAlarmsInvalid", alarm_problem,
                        "Say so: an administrator needs to set ABORT_ALARMS to one alarm and redeploy the agent.")
    if tool_name in _PATCH_TOOLS + _ROLLBACK_TOOLS:
        busy = _busy_refusal(params)
        if busy:
            return busy
    if tool_name in _PATCH_TOOLS:
        refused, promotion_warnings, promotion_missing, promotion_ended = _promotion_check(params)
        if refused:
            return refused
        limit_warnings += promotion_warnings
    if tool_name in _ROLLBACK_TOOLS:
        unreported = _unreported_refusal(params)
        if unreported:
            return unreported
        try:
            rollback_run = rollback_window.last_patch_run(
                instance_ids=params.get("instance_ids"),
                environment=None if params.get("instance_ids") else _normalize_environment(params.get("environment") or ""))
        except rollback_window.ReportsUnreadable as e:
            return _refusal("ReportsUnreadable", f"{e}, so Patchy can't tell which run is the last one to undo.",
                            "Say so, and suggest trying again in a few minutes.")
        no_run = _refusal("NoPatchRun", "No finished Patchy patch run in the last 30 days covers these targets.",
                          "Say so. Rollback only undoes changes from a recorded Patchy patch run, so "
                          "there is nothing it can safely undo here.")
        if not rollback_run:
            return no_run
        # An environment rollback undoes that run's window only in the accounts it patched:
        # elsewhere the same window could hold changes Patchy didn't make.
        if tool_name == "multi_account_rollback" and rollback_run.get("account_ids"):
            ran_in = set(rollback_run["account_ids"])
            keep = [a for a in params.get("account_ids") or [] if a in ran_in]
            dropped = [a for a in params.get("account_ids") or [] if a not in ran_in]
            if not keep:
                return no_run
            if dropped:
                limit_warnings.append(f"Left out {', '.join(dropped)}: the last {params.get('environment')} "
                                      f"patch run didn't patch them.")
            params["account_ids"] = keep
            if rollback_run.get("regions"):
                params["regions"] = [r for r in (params.get("regions") or rollback_run["regions"])
                                     if r in rollback_run["regions"]] or rollback_run["regions"]
        params["since"], params["until"] = rollback_run["since"], rollback_run["until"]
    if tool_name == "create_maintenance_window":
        params["reboot_option"] = reboot_option(_normalize_environment(params.get("target_environment") or "") or "prod")
    dry_run_scan = None
    if tool_name in _PATCH_TOOLS:
        reboot_source = _resolve_reboot(params)
        refused, scope_warnings, cve_coverage = _scope_to_cve(params)
        if refused:
            return refused
        limit_warnings += scope_warnings
        if params.get("scan_execution_id"):
            # The scan gate runs now, not only when the plan runs (2 Nov review, M12).
            refused, dry_run_scan = _scan_check(params["scan_execution_id"], **_scan_targets(tool_name, params))
            if refused:
                return refused
    preview = impl(**params, confirm_execute=False)
    if not isinstance(preview, dict) or preview.get("status") != "confirmation_required":
        return preview
    execution_plan = preview.get("execution_plan") or {}
    if dry_run_scan:
        execution_plan["dry_run_scan"] = dry_run_scan
    if tool_name in _PATCH_TOOLS + _ROLLBACK_TOOLS + ("create_maintenance_window",):
        _store_controls(params, params.get("environment") or execution_plan.get("environment"))
    coverage_text = (f" on every scoped instance in {params.get('environment')} that has them; "
                     f"Inspector lists {cve_coverage[0]} of {cve_coverage[1]} as affected" if cve_coverage else "")
    view = _view(tool_name, params, execution_plan, reboot_source, rollback_run, promotion_missing, coverage_text,
                 promotion_ended)
    if view:
        # Warnings the checks already show (the scan and the earlier stage) aren't repeated.
        warnings = limit_warnings + ([] if tool_name == "create_maintenance_window" else _card_warnings(
            params, execution_plan, "rollback" in tool_name, rollback_run))
        view["notes"] = [w for w in warnings if w != _NO_SCAN_WARNING
                         and not (w.startswith("No ") and " run with the same patch scope " in w)]
    record = _waiting_plan(tool_name, params) or plans.create_plan(
        tool_name, params, execution_plan, get_session_id(), get_operator(), view=view)
    _remember_plan(tool_name, params, record)
    card = _card(tool_name, record, params, execution_plan, reboot_source, rollback_run)
    card["warnings"] = limit_warnings + card["warnings"]
    if coverage_text:
        for field in card["fields"]:
            if field["label"] == "Installs":
                field["value"] += coverage_text
    # The card carries a short record; the UI loads the full one (every target) from
    # GET /api/plans/{id}, which keeps large plans under the card size limit.
    card["record"] = {k: view[k] for k in ("kind", "headline", "summary")} if view else None
    return {
        "status": "plan_ready",
        "result_type": "plan_created",
        "plan_id": record["plan_id"],
        "expires_at": record["expires_at"],
        # The stored decision label (EMERGENCY or SCHEDULED) stays out of what the model reads;
        # why_now says why it runs now in words the operator can use.
        "plan": {k: v for k, v in execution_plan.items() if k != "decision"},
        "next_action": _PLAN_NEXT_ACTION,
        "display": card,
    }


@tool
def execute_patch_operation(
    instance_ids: List[str],
    execution_mode: str,
    environment: str,
    severity_filter: Optional[str] = None,
    scan_execution_id: Optional[str] = None,
    force_emergency: bool = False,
    cve_id: Optional[str] = None,
    severity: Optional[str] = None,
    cvss_score: Optional[float] = None,
    additional_cve_ids: Optional[List[str]] = None,
    pre_patch_state: Optional[Dict[str, Any]] = None,
    reboot: Optional[bool] = None,
) -> dict:
    """Plan a patch of specific instances. Returns a plan for the operator to approve; never executes.

    Use when the operator names instance IDs (i-xxx). Call it directly with the CVE and
    environment; it checks the SLA and windows itself. For environment, severity or CVE scope
    without IDs use multi_account_execute. If a window meets the SLA it returns SCHEDULED;
    pass force_emergency=True only when the operator chooses to patch now.

    Args:
        instance_ids: Instance IDs the operator named.
        execution_mode: "immediate".
        environment: dev, staging or prod.
        severity_filter: CRITICAL, HIGH, MEDIUM or LOW and above. Only when the operator asks for a
            severity level; leave it empty for a CVE so only that CVE's packages install.
        scan_execution_id: Execution ID of a prior dry-run, if any.
        force_emergency: Operator chose to patch now instead of the window.
        cve_id: CVE being remediated. Without a severity_filter, installs only its packages.
        severity: CVE severity; sets the SLA.
        cvss_score: CVSS score.
        additional_cve_ids: Other CVEs fixed by the same patch.
        pre_patch_state: Output of capture_patch_state.
        reboot: True only if the operator asks to reboot, False if they say not to. Omit to use
            the environment's reboot policy.
    """
    params = {k: v for k, v in locals().items()}
    return _plan_or_pass("execute_patch_operation", _execute_patch_operation_impl, params)


@tool
def rollback_patches(instance_ids: List[str]) -> dict:
    """Plan a rollback of specific instances' last Patchy patch run. Returns a plan for approval.

    Undoes the yum transactions that run made, not later changes. Refused when no recorded run
    covers the instances.

    Use when the operator names instance IDs. For an environment use multi_account_rollback.

    Args:
        instance_ids: Instance IDs to roll back.
    """
    return _plan_or_pass("rollback_patches", _rollback_patches_impl, {"instance_ids": instance_ids})


@tool
def multi_account_execute(environment: str,
                          account_ids: List[str],
                          scan_execution_id: Optional[str] = None,
                          regions: Optional[List[str]] = None,
                          severity_filter: Optional[str] = None,
                          cve_id: Optional[str] = None,
                          severity: Optional[str] = None,
                          cvss_score: Optional[float] = None,
                          additional_cve_ids: Optional[List[str]] = None,
                          pre_patch_state: Optional[Dict[str, Any]] = None,
                          reboot: Optional[bool] = None,
                          force_emergency: bool = False) -> dict:
    """Plan a patch of an environment across accounts. Returns a plan for approval; never executes.

    Use when the operator describes scope by environment, severity or CVE. Call
    resolve_execution_scope first and pass its accounts; there's no need to look up the
    CVE or findings first, the plan checks the SLA itself. If a window meets the SLA it
    returns SCHEDULED; pass force_emergency=True only when the operator chooses to patch now.
    Rollout limits come from the environment's policy.

    Args:
        environment: dev, staging or prod.
        account_ids: Accounts from resolve_execution_scope.
        scan_execution_id: Execution ID of a prior dry-run, if any.
        regions: Regions; defaults to the configured ones.
        severity_filter: CRITICAL, HIGH, MEDIUM or LOW and above. Only when the operator asks for a
            severity level; leave it empty for a CVE so only that CVE's packages install.
        cve_id: CVE being remediated. Without a severity_filter, installs only its packages.
        severity: CVE severity; sets the SLA.
        cvss_score: CVSS score.
        additional_cve_ids: Other CVEs fixed by the same patch.
        pre_patch_state: Output of capture_patch_state.
        reboot: True only if the operator asks to reboot, False if they say not to. Omit to use
            the environment's reboot policy.
        force_emergency: Operator chose to patch now instead of the window.
    """
    params = {k: v for k, v in locals().items()}
    return _plan_or_pass("multi_account_execute", _multi_account_execute_impl, params)


@tool
def multi_account_rollback(environment: str,
                           account_ids: List[str],
                           regions: Optional[List[str]] = None) -> dict:
    """Plan a rollback of an environment's last Patchy patch run across accounts. Returns a plan for approval.

    Call resolve_execution_scope first. Rollout limits come from the environment's policy.

    Args:
        environment: dev, staging or prod.
        account_ids: Accounts from resolve_execution_scope.
        regions: Regions; defaults to the configured ones.
    """
    params = {k: v for k, v in locals().items()}
    return _plan_or_pass("multi_account_rollback", _multi_account_rollback_impl, params)


@tool
def create_maintenance_window(name: str, schedule: str, duration: int, target_environment: str) -> dict:
    """Plan a new maintenance window for an environment. Returns a plan for approval.

    Args:
        name: Window name.
        schedule: cron(...) or at(...) expression.
        duration: Hours, 1-24.
        target_environment: dev, staging or prod.
    """
    params = {k: v for k, v in locals().items()}
    return _plan_or_pass("create_maintenance_window", _create_maintenance_window_impl, params)


EXECUTORS: Dict[str, Callable[[dict], dict]] = {
    "create_maintenance_window": lambda p: _create_maintenance_window_impl(**p, confirm_execute=True),
    "execute_patch_operation": lambda p: _execute_patch_operation_impl(**p, confirm_execute=True),
    "rollback_patches": lambda p: _rollback_patches_impl(**p, confirm_execute=True),
    "multi_account_execute": lambda p: _multi_account_execute_impl(**p, confirm_execute=True),
    "multi_account_rollback": lambda p: _multi_account_rollback_impl(**p, confirm_execute=True),
}


def run_approved_plan(plan_id: str, session_id: str) -> dict:
    """Claim an approved plan and execute its stored parameters.

    Args:
        plan_id: Plan to run.
        session_id: Conversation the approval came from.

    Returns:
        {plan, approval, result} on success, or {error, error_code} when the plan can't run.
    """
    def refused(reason: str) -> dict:
        logger.warning(f"[PLAN] refused {plan_id}: {reason}")
        return {"error": reason, "error_code": "PlanNotRunnable"}

    # Checks that don't need the claim run first, so a refusal here leaves the
    # approval unused: the plan can run once the reason is fixed, while it's fresh.
    # The Change Calendar may have closed since the plan was approved.
    freeze = change_controls.calendar_closed()
    if freeze:
        return refused(freeze)
    try:
        stored = plans.load_plan(plan_id)
    except plans.PlanError as e:
        return refused(str(e))
    if EXECUTORS.get(stored.get("tool")) is None:
        return refused(f"unknown plan tool {stored.get('tool')}")
    lock = None
    if stored.get("tool") in _PATCH_TOOLS + _ROLLBACK_TOOLS:
        # Two overlapping plans approved seconds apart would both pass the run guard, because
        # a run's pending report is written after it starts. One start at a time closes that
        # gap (2 Nov review, T9).
        try:
            lock, busy = run_guard.take_start_lock(plan_id)
        except Exception as e:
            logger.warning(f"[RUN_GUARD] start lock not taken for {plan_id}: {e}")
            return refused("Patchy couldn't take the start lock, so it can't be sure another plan isn't "
                           "starting on the same instances. Try again in a minute.")
        if busy:
            return refused(busy)
    try:
        return _check_and_run(plan_id, session_id, stored, refused)
    finally:
        if lock:
            run_guard.release_start_lock(lock, plan_id)


def _check_and_run(plan_id: str, session_id: str, stored: dict, refused: Callable[[str], dict]) -> dict:
    """The rest of run_approved_plan, under the start lock: the run guard, the claim, the run."""
    # Checked again at the start: a run may have begun since the plan was made.
    if stored.get("tool") in _ROLLBACK_TOOLS:
        unreported = _unreported_refusal(stored.get("params") or {})
        if unreported:
            return refused(unreported["error"] + " Try again once it's written.")
    if stored.get("tool") in _PATCH_TOOLS + _ROLLBACK_TOOLS:
        run = run_guard.blocking_run(_run_scope(stored.get("params") or {}))
        if run:
            return refused(f"{run_guard.describe(run)}, and it covers instances this plan targets. "
                           "Try again once it has finished.")
    # A CVE-scoped plan installs exactly its approved package list: rewrite the
    # override file from that list now, so nothing written since approval counts.
    packages = (stored.get("params") or {}).get("install_packages")
    if packages:
        try:
            url = cve_scope.write_install_override(packages)
        except ValueError as e:
            return refused(str(e))
        if url != stored["params"].get("install_override_url"):
            return refused("install list doesn't match the approved plan")
    try:
        plan, approval = plans.claim_plan(plan_id, session_id)
    except plans.PlanError as e:
        return refused(str(e))
    # The approver is the operator of record for the run and its audit trail.
    set_operator(approval.get("approver") or "unknown")
    logger.info(f"[PLAN] executing {plan_id} tool={plan['tool']} approver={approval.get('approver')} "
                f"at={datetime.now(timezone.utc).isoformat()}")
    # The run uses the limits and alarms the plan stored; each start is recorded
    # the moment it returns, so a later failure can't leave a run untracked.
    params = dict(plan["params"])
    rollout, alarms = params.pop("rollout", None), params.pop("abort_alarms", None)
    recorded: Dict[str, bool] = {}

    def on_started(exec_id: str) -> None:
        recorded[exec_id] = _record_execution(exec_id, plan_id, plan, approval)

    from ._shared import plan_controls
    controls: dict = {}
    try:
        with plan_controls(plan_id, rollout, alarms, on_started) as controls:
            # Why the plan runs now, as its preview found, for the run's report (2 Nov review, M16).
            controls["run_now_reason"] = (plan.get("summary") or {}).get("run_now_reason")
            result = EXECUTORS[plan["tool"]](params) or {}
    except Exception as e:  # the claim is spent; the operator still needs to see what happened
        logger.exception(f"[PLAN] executor failed for {plan_id}")
        result = {"status": "FAILED", "error": f"The run failed to start: {str(e)[:200]}",
                  "automation_execution_ids": list(recorded)}
    unrecorded, listed = _record_executions(plan_id, plan, approval, result, recorded)
    return {"plan": plan, "approval": approval, "result": result, "unrecorded": unrecorded,
            "unlisted": not listed,
            "unreported": list(controls.get("unreported") or [])}


def _execution_ids(result: dict) -> List[str]:
    ids = result.get("automation_execution_ids") or [result.get("automation_execution_id")]
    return [i for i in ids if i]


def _record_execution(exec_id: str, plan_id: str, plan: dict, approval: dict) -> bool:
    """Record which conversation started an execution, so the UI can show and stop only its own runs."""
    try:
        plans.put_record(f"run-executions/{exec_id}.json",
                         {"execution_id": exec_id, "plan_id": plan_id, "session_id": plan.get("session_id"),
                          "approver": approval.get("approver"), "tool": plan.get("tool"),
                          "started_at": datetime.now(timezone.utc).isoformat()},
                         create_only=True)
        return True
    except Exception as e:
        logger.warning(f"[PLAN] could not record execution {exec_id}: {e}")
        return False


def _record_executions(plan_id: str, plan: dict, approval: dict, result: dict,
                       recorded: Optional[Dict[str, bool]] = None) -> Tuple[List[str], bool]:
    """Record any execution not recorded at start, and the plan's full list of executions.

    The list (plan-executions/<plan_id>.json) lets verification check every
    location a plan started, not just the one it was asked about.

    Returns:
        (execution IDs that couldn't be recorded, whether the plan's list was saved).
    """
    recorded = recorded or {}
    ids = list(dict.fromkeys(list(recorded) + _execution_ids(result)))
    unrecorded = []
    for exec_id in ids:
        if exec_id not in recorded:
            recorded[exec_id] = _record_execution(exec_id, plan_id, plan, approval)
        if not recorded[exec_id]:
            unrecorded.append(exec_id)
    listed = True
    if ids:
        try:
            plans.put_record(f"plan-executions/{plan_id}.json",
                             {"plan_id": plan_id, "execution_ids": ids, "tool": plan.get("tool")},
                             create_only=True)
        except Exception as e:
            logger.warning(f"[PLAN] could not record the executions of {plan_id}: {e}")
            listed = False
    return unrecorded, listed


_EXEC_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _stop_execution(execution_id: str) -> dict:
    from .patch_tools import emergency_stop
    return emergency_stop(automation_execution_id=execution_id)


def _plan_run_ids(execution_id: str, rec: dict, session_id: str) -> List[str]:
    """This execution first, then the other executions its plan started from this conversation."""
    ids = [execution_id]
    listed = plans.get_record(f"plan-executions/{rec['plan_id']}.json") if rec.get("plan_id") else None
    for other in (listed or {}).get("execution_ids") or []:
        if other in ids or not isinstance(other, str) or not _EXEC_ID_RE.match(other):
            continue
        sibling = plans.get_record(f"run-executions/{other}.json")
        if sibling and sibling.get("session_id") == session_id and sibling.get("plan_id") == rec.get("plan_id"):
            ids.append(other)
    return ids


def _already_finished(out: dict) -> bool:
    return "InvalidAutomationStatusUpdate" in f"{out.get('error_code', '')} {out.get('error', '')}"


def stop_run(execution_id: str, session_id: str) -> dict:
    """Stop a run this conversation started, in every account and Region. Deterministic: no model involved.

    A plan by instance ID starts one execution per account and Region, so Stop
    cancels each execution the plan started from this conversation.

    Args:
        execution_id: Parent Automation execution ID from the run card.
        session_id: Conversation the stop request came from.

    Returns:
        {results, stopped, finished, failed}, or {error, error_code} when nothing could be stopped.
    """
    if not isinstance(execution_id, str) or not _EXEC_ID_RE.match(execution_id):
        return {"error": "run not found", "error_code": "RunNotFound"}
    rec = plans.get_record(f"run-executions/{execution_id}.json")
    if not rec or rec.get("session_id") != session_id:
        return {"error": "run not found for this conversation", "error_code": "RunNotFound"}
    ids = _plan_run_ids(execution_id, rec, session_id)
    logger.info(f"[PLAN] stop requested for {ids} (plan {rec.get('plan_id')}) by {get_operator()}")
    results, stopped, finished, failed = [], [], [], []
    for exec_id in ids:
        out = _stop_execution(exec_id)
        if "error" not in out:
            results.extend(out.get("results") or [])
            stopped.append(exec_id)
        elif _already_finished(out):
            finished.append(exec_id)
        else:
            failed.append({"execution_id": exec_id, "error": str(out["error"])})
    if failed and not stopped and not finished:
        return {"error": failed[0]["error"], "error_code": "StopFailed", "failed": failed}
    return {"results": results, "stopped": stopped, "finished": finished, "failed": failed,
            "execution_ids": ids, "operator": get_operator()}


def stop_card(execution_id: str, out: dict) -> dict:
    """Card for the outcome of a Stop request."""
    if "error" in out:
        return {"type": "run", "execution_ids": [execution_id], "status": "stop_failed",
                "title": "Could not stop the run", "subtitle": execution_id,
                "fields": [{"label": "Reason", "value": str(out["error"]), "tone": "bad"}]}
    ids = out.get("execution_ids") or [execution_id]
    stopped = out.get("stopped", ids)
    if not stopped and out.get("finished"):
        return {"type": "run", "execution_ids": ids, "status": "finished", "title": "The run had already finished",
                "subtitle": execution_id,
                "fields": [{"label": "Status", "value": "Nothing to stop: every location had already finished."}]}
    fields = [{"label": "Status", "value": (f"Cancel requested in {len(stopped)} location"
                                            f"{'' if len(stopped) == 1 else 's'}. Instances already patching "
                                            "finish; the rest don't start."), "tone": "warn"}]
    if out.get("finished"):
        fields.append({"label": "Already finished", "value": ", ".join(out["finished"])})
    for f in out.get("failed") or []:
        fields.append({"label": "Not stopped", "value": f"{f['execution_id']}: {f['error']}", "tone": "bad"})
    fields.append({"label": "Stopped by", "value": get_operator()})
    return {"type": "run", "execution_ids": ids, "status": "stopping",
            "title": "Stopping the run", "subtitle": execution_id, "fields": fields}


def run_card(plan_id: str, ran: dict) -> dict:
    """Card describing the outcome of starting an approved plan."""
    if "error" in ran:
        return {"type": "run", "title": "Plan could not run", "subtitle": f"Plan {plan_id[:6]}",
                "fields": [{"label": "Reason", "value": ran["error"], "tone": "bad"}]}
    plan, approval, result = ran["plan"], ran["approval"], ran["result"] or {}
    ids = _execution_ids(result)
    started = result.get("status") == "EXECUTING" or bool(ids)
    fields = [
        {"label": "Status", "value": "Started" if started else str(result.get("error") or result.get("status")),
         "tone": "ok" if started else "bad"},
        {"label": "Approved by", "value": str(approval.get("approver"))},
    ]
    if ids:
        fields.append({"label": "Executions", "value": ", ".join(ids)})
    if result.get("instance_count") is not None:
        fields.append({"label": "Instances", "value": str(result["instance_count"])})
    if ran.get("unrecorded"):
        fields.append({"label": "Stop", "value": (f"{', '.join(ran['unrecorded'])} can't be stopped from this card "
                                                  "(the run record wasn't saved). Stop it in the SSM console."),
                       "tone": "warn"})
    if ran.get("unlisted") and len(ids) > 1:
        fields.append({"label": "Stop", "value": ("The list of this plan's locations couldn't be saved, so Stop and "
                                                  "verification see one location at a time. Stop each location in the "
                                                  "SSM console if needed."), "tone": "warn"})
    if ran.get("unreported"):
        fields.append({"label": "Report", "value": (f"{', '.join(ran['unreported'])} won't get a compliance report: "
                                                    "its context couldn't be saved. The run itself continues."),
                       "tone": "warn"})
    not_started = result.get("not_started") or []
    if not_started:
        fields.append({"label": "Not started", "value": "; ".join(
            f"{n.get('account')} {n.get('region')} ({len(n.get('instance_ids') or [])} instance(s)): {n.get('error')}"
            for n in not_started), "tone": "bad"})
        fields.append({"label": "Next", "value": ("This plan's approval is spent, so it can't run again. "
                                                  "Make a new plan for the locations that didn't start."),
                       "tone": "warn"})
    title = ("Partly started" if not_started else "Running") if started else "Did not start"
    return {"type": "run", "plan_id": plan_id, "title": f"{title} · plan {plan_id[:6]}",
            "subtitle": plan.get("summary", {}).get("environment") or "", "fields": fields,
            "execution_ids": ids, "headline": (plan.get("view") or {}).get("headline"),
            "approved_by": approval.get("approver"), "not_started": len(not_started),
            "actions": [{"label": "Check status", "prompt": f"Check status for {ids[0]}"}] if ids else []}


def run_note(plan_id: str, ran: dict) -> str:
    """Short, readable record of the run for the conversation and the model."""
    if "error" in ran:
        return f"Approved plan {plan_id[:6]} could not run: {ran['error']}. Tell me in one sentence."
    result = ran["result"] or {}
    ids = ", ".join(_execution_ids(result)) or "none"
    status = result.get("status") or result.get("error") or "unknown"
    return (f"I approved plan {plan_id[:6]} ({ran['plan']['tool']}). Status: {status}. Executions: {ids}. "
            f"Confirm what started in one or two sentences; nothing else to run.")
