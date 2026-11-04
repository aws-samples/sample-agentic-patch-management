"""The change record a plan stores: what it changes, where, when, how it stops and how it's undone.

Built once when the plan is made and stored with it, outside the plan hash, so
the UI can show the record in full: every target with its name, account and
Region, the reach by location, and the checks the plan made. Checks report
only what was checked; anything not checked is information, not a pass.
"""

import contextvars
import logging
import os
from helper.concurrency import ContextThreadPoolExecutor as ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from helper import change_controls, rollback_window

logger = logging.getLogger(__name__)

MAX_TARGETS = 500
_ALARM_WORKERS = 8
_INSTANCE_TOOLS = ("execute_patch_operation", "rollback_patches")


def _fleet_ids() -> set:
    from helper.tools import _shared
    return set(_shared.fleet_instances())


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _env(tool: str, params: dict, execution_plan: dict) -> str:
    from helper.tools._shared import _normalize_environment
    raw = (execution_plan.get("environment") or params.get("environment")
           or params.get("target_environment") or "")
    return _normalize_environment(raw) if raw else ""


def _targets(tool: str, params: dict, execution_plan: dict, env: str,
             rollback_run: Optional[dict]) -> Tuple[List[str], List[Dict[str, object]]]:
    """(instance IDs, locations) the plan reaches.

    Instance plans reach the locations their instances are in. Environment plans
    reach every account and Region they name, with zero where nothing is tagged yet.
    """
    from helper.tools import _shared
    if tool in _INSTANCE_TOOLS:
        ids = list(execution_plan.get("instance_ids") or params.get("instance_ids") or [])
        groups = _shared._locate_instances(ids) if ids else {}
        locations = [{"account": a, "region": r, "instances": len(v)} for (a, r), v in groups.items()]
        return ids, sorted(locations, key=lambda loc: (str(loc["account"]), str(loc["region"])))
    _shared._get_fleet_summary()
    cache = _shared.fleet_instances()
    if tool == "create_maintenance_window":
        from helper.cross_account import get_hub_account_id
        accounts, regions = [get_hub_account_id()], [_shared.AWS_REGION]
    else:
        accounts = list(execution_plan.get("accounts") or params.get("account_ids") or [])
        regions = list(execution_plan.get("regions") or params.get("regions") or _shared.SPOKE_REGIONS)
    # The same instances the plan's checks used: the fleet view, plus new ones EC2 reports.
    where = (_shared.environment_instances(env, accounts, regions) if tool != "create_maintenance_window"
             else {i: {"account_id": v.get("account_id"), "region": v.get("region")} for i, v in cache.items()
                   if v.get("managed") and _shared._normalize_environment(v.get("environment") or "") == env
                   and v.get("account_id") in accounts and v.get("region") in regions})
    ids = list(where)
    if rollback_run and rollback_run.get("instance_ids"):
        patched = set(rollback_run["instance_ids"])
        ids = [i for i in ids if i in patched]
    locations = [{"account": a, "region": r,
                  "instances": sum(1 for i in ids if where[i].get("account_id") == a and where[i].get("region") == r)}
                 for a in accounts for r in regions]
    return ids, locations


def _last_scan(ids: List[str]) -> Optional[dict]:
    """When Systems Manager last scanned these instances: {at, first_at, scanned, targets}, or None.

    at is the most recent scan and first_at the oldest of each instance's latest, so a fleet
    where some instances haven't been scanned for weeks doesn't read as just scanned (M22).
    """
    from helper.tools import _shared
    from helper.cross_account import get_client
    from helper.retry import throttled
    if not ids:
        return None
    latest, oldest, scanned = None, None, 0
    try:
        for (account, region), group in _shared._locate_instances(ids[:MAX_TARGETS]).items():
            ssm = get_client("ssm", account_id=account, region=region)
            for n in range(0, len(group), 50):
                batch = group[n:n + 50]
                states = throttled(lambda: ssm.describe_instance_patch_states(InstanceIds=batch))
                for state in states.get("InstancePatchStates") or []:
                    end = state.get("OperationEndTime")
                    if end is None:
                        continue
                    end = end if getattr(end, "tzinfo", None) else end.replace(tzinfo=timezone.utc)
                    scanned += 1
                    latest = max(latest or end, end)
                    oldest = min(oldest or end, end)
    except Exception as e:
        logger.info(f"[PLAN_VIEW] last scan time unavailable: {e}")
        return None
    if not latest:
        return None
    return {"at": latest.isoformat(), "first_at": oldest.isoformat(), "scanned": scanned,
            "targets": min(len(ids), MAX_TARGETS)}


def _target_rows(ids: List[str]) -> List[Dict[str, object]]:
    """Each target with its name and location; pending_sync when Explorer doesn't list it yet."""
    from helper.tools import _shared
    cache = _shared.fleet_instances()
    missing = [i for i in ids[:MAX_TARGETS] if i not in cache]
    direct: Dict[str, dict] = {}
    if missing:
        # From EC2 directly: the location, and the Name tag Explorer would have given.
        try:
            direct = _shared._direct_describe_scoped_instances(instance_ids=missing)
        except Exception as e:
            logger.info(f"[PLAN_VIEW] direct lookup of {len(missing)} target(s) failed: {e}")
        unplaced = [i for i in missing if i not in direct]
        for (account, region), group in (_shared._locate_instances(unplaced).items() if unplaced else []):
            for i in group:
                direct[i] = {"account_id": account, "region": region}
    rows = []
    for iid in ids[:MAX_TARGETS]:
        info = cache.get(iid) or direct.get(iid) or {}
        rows.append({"instance_id": iid, "name": info.get("name") or "",
                     "account": info.get("account_id") or "", "region": info.get("region") or "",
                     "pending_sync": iid not in cache})
    return rows


def _alarm_states(account: str, region: str, names: List[str]) -> Dict[str, str]:
    """{alarm name: state} for the named alarms that exist in one account and Region."""
    from helper.cross_account import get_client
    cw = get_client("cloudwatch", account_id=account, region=region)
    out: Dict[str, str] = {}
    for page in cw.get_paginator("describe_alarms").paginate(AlarmNames=names,
                                                             AlarmTypes=["MetricAlarm", "CompositeAlarm"]):
        for alarm in page.get("MetricAlarms", []) + page.get("CompositeAlarms", []):
            out[alarm["AlarmName"]] = alarm.get("StateValue", "")
    return out


def _first(items: List[str], n: int = 10) -> str:
    return ", ".join(items[:n]) + (f" and {len(items) - n} more" if len(items) > n else "")


def _alarm_check(alarms: List[str], locations: List[Dict[str, object]]) -> dict:
    """Whether each location has the abort alarms: a location without one stops its run at the start."""
    if not alarms:
        return {"key": "alarms", "label": "Stop alarm", "state": "info",
                "detail": "No abort alarm is configured; only Stop run halts this change."}
    places = [(str(loc["account"]), str(loc["region"])) for loc in locations if loc.get("account")]

    def read(place):
        try:
            return place, _alarm_states(place[0], place[1], alarms), None
        except Exception as e:
            logger.warning(f"[PLAN_VIEW] alarms unreadable in {place}: {e}")
            return place, None, str(e)

    # Each read runs in the caller's context, so spoke calls carry the operator's session name.
    with ThreadPoolExecutor(max_workers=max(1, min(_ALARM_WORKERS, len(places)))) as pool:
        contexts = [contextvars.copy_context() for _ in places]
        results = list(pool.map(lambda pc: pc[1].run(read, pc[0]), zip(places, contexts)))
    missing, firing, unreadable = [], [], []
    for (account, region), states, error in results:
        where = f"{account} {region}"
        if error is not None:
            unreadable.append(where)
            continue
        for name in alarms:
            if name not in states:
                missing.append(f"{name} in {where}")
            elif states[name] == "ALARM":
                firing.append(f"{name} in {where}")
    problems = []
    if missing:
        problems.append("missing: " + _first(missing) + " (the run there stops at the start)")
    if firing:
        problems.append("in ALARM now: " + _first(firing) + " (the run there stops at once)")
    if unreadable:
        problems.append("couldn't be read in " + _first(unreadable))
    if problems:
        return {"key": "alarms", "label": "Stop alarm", "state": "warn", "detail": "; ".join(problems) + "."}
    return {"key": "alarms", "label": "Stop alarm present", "state": "pass",
            "detail": f"{', '.join(alarms)} found in every target account and Region, none in ALARM."}


def _when(tool: str, params: dict, execution_plan: dict) -> Tuple[str, Optional[dict]]:
    """(When text, the SLA check) for an install."""
    sla, source = execution_plan.get("sla_hours"), execution_plan.get("sla_source") or "default"
    if params.get("force_emergency"):
        return ("Now, as you asked, instead of waiting for a maintenance window.",
                {"key": "sla", "label": "Window and SLA", "state": "pass",
                 "detail": "You asked to patch now" + (f"; the SLA is {sla} h ({source})." if sla else ".")})
    if sla:
        # The plan names the windows it passed over and why (helper.tools._shared._window_check_text),
        # or how many targets no schedule patches in time (helper.coverage.table_text).
        detail = execution_plan.get("window_check") or f"No maintenance window meets the {sla} h SLA ({source}), so it runs now."
        rows = (execution_plan.get("coverage") or {}).get("rows") or []
        if not rows and "couldn't" in detail:
            # The schedules weren't checked at all (2 Nov review, M13): don't claim none qualifies.
            return (f"Now. Patchy couldn't confirm whether a schedule patches these targets before the {sla} h SLA "
                    f"deadline ({source}).",
                    {"key": "sla", "label": "Window and SLA", "state": "info", "detail": detail})
        if any(r.get("covered") for r in rows):
            return (f"Now. Some targets aren't scheduled to be patched before the {sla} h SLA deadline ({source}).",
                    {"key": "sla", "label": "Window and SLA", "state": "pass", "detail": detail})
        return (f"Now. No maintenance window or patch policy patches these targets before the {sla} h SLA "
                f"deadline ({source}).",
                {"key": "sla", "label": "Window and SLA", "state": "pass", "detail": detail})
    if params.get("severity_filter"):
        # Each finding has its own SLA clock, from when it was first seen, so no one window can be
        # shown to meet them all; waiting could miss the oldest. (30 Oct live: it read "No SLA applies".)
        return ("Now. A severity plan covers findings first seen at different times, so it isn't held for a "
                "maintenance window.",
                {"key": "sla", "label": "Window and SLA", "state": "info",
                 "detail": "Severity plans run now: each finding's SLA runs from when it was first seen, so "
                           "Patchy can't show one window meets them all. Plan by CVE to have windows and patch "
                           "policies checked against that CVE's deadline."})
    return "Now.", {"key": "sla", "label": "Window and SLA", "state": "info",
                    "detail": "No SLA applies, so it runs now."}


def _rollout_text(tool: str, params: dict, env: str) -> str:
    from helper.cross_account import execution_limits
    limits = params.get("rollout") or execution_limits(env or "prod")
    if tool == "create_maintenance_window":
        return (f"{limits['send_command_max_concurrency']} of the instances at a time each window, "
                f"stopping after {limits['send_command_max_errors']} fail.")
    if tool in ("multi_account_execute", "multi_account_rollback"):
        accounts = (params.get("max_concurrency") or limits["account_max_concurrency"],
                    params.get("max_errors") or limits["account_max_errors"])
        instances = (limits["send_command_max_concurrency"], limits["send_command_max_errors"])
    else:
        accounts = (limits["account_max_concurrency"], limits["account_max_errors"])
        instances = (limits["instance_max_concurrency"], limits["instance_max_errors"])
    return (f"{env or 'Environment'} policy: {accounts[0]} account and Region locations at a time, stopping "
            f"after {accounts[1]} fail; within each, {instances[0]} of the instances at a time, stopping after "
            f"{instances[1]} fail.")


def _scan_check(scan_id: str, scan: dict, n: int) -> dict:
    """The dry-run check: what the plan checked about the scan when it was made."""
    at = (datetime.fromisoformat(scan["at"]).astimezone(timezone.utc).strftime(" at %H:%M UTC")
          if scan.get("at") else "")
    check = {"key": "scan", "label": "Dry-run scan", "state": "pass",
             "detail": f"Scan {scan_id[:8]} ran first: it succeeded{at} on these targets.",
             **({"scan_at": scan["at"]} if scan.get("at") else {})}
    if scan.get("missed"):
        check.update(state="warn", label="Dry-run scan missed targets",
                     detail=f"Scan {scan_id[:8]} succeeded{at} but didn't scan {scan['missed']} of the {n} targets.")
    elif scan.get("by_tag") is not None:
        check.update(state="warn", label="Dry-run scan not confirmed",
                     detail=f"Scan {scan_id[:8]} succeeded{at} on the {scan['by_tag'] or 'tagged'} environment by tag. "
                            "Its record doesn't list instances, so Patchy can't confirm it covered these targets.")
    return check


def _reboot_text(option: str, source: str) -> str:
    if option == "RebootIfNeeded":
        return f"Reboots if needed ({source}), once the install finishes, so new kernels and libraries take effect."
    return (f"No reboot ({source}). Kernel and library fixes take effect at the next reboot; "
            "until then Inspector may still report them.")


def build(tool: str, params: dict, execution_plan: dict, *, reboot_source: str = "",
          rollback_run: Optional[dict] = None, promotion_missing: Optional[str] = None,
          promotion_ended: Optional[datetime] = None) -> dict:
    """The change record for a plan.

    Args:
        tool: The write tool the plan runs.
        params: The plan's stored parameters.
        execution_plan: The tool's preview of the run.
        reboot_source: Where the reboot choice came from ("dev policy", "requested").
        rollback_run: For a rollback, the patch run it undoes.
        promotion_missing: The earlier stage not verified for this scope, if any.
        promotion_ended: When the earlier stage's verified run for this scope ended, if known.

    Returns:
        {kind, headline, summary, environment, checks, sections, locations, targets, targets_total}.
    """
    from helper.tools._shared import SCOPE_TAG_KEY, SCOPE_TAG_VALUE
    env = _env(tool, params, execution_plan)
    kind = "window" if tool == "create_maintenance_window" else "rollback" if "rollback" in tool else "patch"
    ids, locations = _targets(tool, params, execution_plan, env, rollback_run)
    # Count where the instances are; the grid still shows every location a tag run reaches
    # (disruption round, 2 Nov: "2 accounts and 3 Regions" for instances in 2 Regions).
    occupied = [loc for loc in locations if loc.get("instances")] or locations
    n, accounts = len(ids), len({loc["account"] for loc in occupied})
    regions = len({loc["region"] for loc in occupied})
    instances = _plural(n, f"{env} instance" if env else "instance")
    reach = f"{_plural(n, 'instance')} in {_plural(accounts, 'account')} and {_plural(regions, 'Region')}"
    checks: List[dict] = []
    sections: List[dict] = []

    tag = f"{SCOPE_TAG_KEY}={SCOPE_TAG_VALUE}"
    if tool in _INSTANCE_TOOLS:
        checks.append({"key": "scope", "label": "In scope", "state": "pass",
                       "detail": (f"The instance carries {tag}" if n == 1 else f"All {n} instances carry {tag}")
                       + " in configured accounts and Regions."})
    else:
        checks.append({"key": "scope", "label": "In scope", "state": "pass",
                       "detail": f"The run targets only instances tagged {tag} and Environment={env}."})
    calendar = os.environ.get("CHANGE_CALENDAR", "").strip()
    # Checked now and when the run starts, not during it (2 Nov review, T7).
    checks.append({"key": "calendar", "label": "Change calendar open", "state": "pass",
                   "detail": f"{calendar} is open now. A run that starts before it closes isn't stopped when it closes."}
                  if calendar else
                  {"key": "calendar", "label": "Change calendar", "state": "info",
                   "detail": "No change calendar is configured."})

    if kind == "window":
        headline = f"Create maintenance window {params.get('name')} for {env}"
        summary = (f"Creates a {params.get('duration')} h window that installs approved patches on {reach} "
                   f"each time it opens.")
        sections += [
            {"key": "what", "label": "What changes",
             "text": "A maintenance window with a patch task that installs approved patches (AWS-RunPatchBaseline)."},
            {"key": "where", "label": "Where", "text": f"Instances tagged Environment={env} in this account and Region."},
            {"key": "when", "label": "When", "text": f"{params.get('schedule')}, for {params.get('duration')} h."},
            {"key": "rollout", "label": "How it rolls out", "text": _rollout_text(tool, params, env)},
            {"key": "reboot", "label": "Reboot", "text": _reboot_text(params.get("reboot_option") or "NoReboot",
                                                                      f"{env} policy")},
            {"key": "undo", "label": "Undo", "text": "Delete the window in Systems Manager to stop future runs."},
        ]
    elif kind == "rollback":
        headline = f"Roll back {instances}"
        run = rollback_run or {}
        when = ""
        if run.get("started_at"):
            when = datetime.fromisoformat(run["started_at"]).astimezone(timezone.utc).strftime(" (%d %b %H:%M UTC")
            when += rollback_window.outcome_text(run) + ")"
        summary = f"Undoes the last Patchy patch run on {reach}, now."
        sections += [
            {"key": "what", "label": "What changes",
             "text": (f"Undoes the yum transactions that began during patch run {str(run.get('execution_id', ''))[:8]}"
                      f"{when} on each instance. Instances that run didn't change are left alone.")},
            {"key": "where", "label": "Where", "text": f"{reach}."},
            {"key": "when", "label": "When", "text": "Now."},
            {"key": "rollout", "label": "How it rolls out", "text": _rollout_text(tool, params, env)},
        ]
    else:
        cve, severity = params.get("cve_id"), params.get("severity_filter")
        if severity:
            headline = f"Patch {instances}, {severity.upper()} and higher"
            what = f"Approved {severity.upper()} and higher patches from the patch baseline."
            checks.append({"key": "packages", "label": "Patch level", "state": "info",
                           "detail": f"Approved {severity.upper()} and higher patches install, not a single CVE's."})
        elif params.get("install_packages"):
            pkgs = params["install_packages"]
            headline = f"Patch {instances} for {cve}"
            what = f"{', '.join(pkgs)}, newest available build, for {cve}. Nothing else installs."
            checks.append({"key": "packages", "label": f"Only {cve}'s packages", "state": "pass",
                           "detail": f"{', '.join(pkgs)}, from Inspector's findings on these instances."})
        else:
            headline = f"Patch {instances}" + (f" for {cve}" if cve else "")
            what = "Every patch the patch baseline approves."
            checks.append({"key": "packages", "label": "Patch level", "state": "info",
                           "detail": "Every approved patch installs."})
        when, sla_check = _when(tool, params, execution_plan)
        checks.append(sla_check)
        option = params.get("reboot_option") or "NoReboot"
        reboot = "Reboots if needed." if option == "RebootIfNeeded" else "No reboot."
        summary = f"Installs on {reach}, {when.split('.')[0].lower()}. {reboot}"
        where = f"{reach}."
        if tool not in _INSTANCE_TOOLS:
            where += " The run targets the tag, so instances tagged later are included too."
        sections += [
            {"key": "what", "label": "What changes", "text": what},
            {"key": "where", "label": "Where", "text": where},
            {"key": "when", "label": "When", "text": when},
            {"key": "rollout", "label": "How it rolls out", "text": _rollout_text(tool, params, env)},
            {"key": "reboot", "label": "Reboot", "text": _reboot_text(option, reboot_source or f"{env} policy")},
        ]
        from helper.change_controls import STAGES
        if env in STAGES[1:] or env not in STAGES:
            previous = STAGES[STAGES.index(env) - 1] if env in STAGES else STAGES[-2]
            detail = change_controls.promotion_text(previous, change_controls.scope_key(params),
                                                    bool(promotion_missing), promotion_ended)
            checks.append({"key": "promotion", "label": f"{previous} verified",
                           "state": "warn" if promotion_missing else "pass", "detail": detail})
        scan = params.get("scan_execution_id")
        if scan:
            checks.append(_scan_check(scan, execution_plan.get("dry_run_scan") or {}, n))
        else:
            # No dry-run for this plan; say when Systems Manager last scanned the targets, so the
            # check doesn't read as "never scanned" (2 Nov: the agent and the card disagreed).
            last = _last_scan(ids)
            check = {"key": "scan", "label": "No dry-run scan", "state": "info",
                     "detail": "Optional; a scan would show the exact versions first."}
            if last:
                check.update(last_scan_at=last["at"], first_scan_at=last["first_at"], scanned=last["scanned"],
                             targets=last["targets"])
            checks.append(check)

    if kind != "window":
        # Targets SSM Explorer doesn't list yet: Patchy read them from EC2, so it has their
        # tags but no patch state. The run includes them (2 Nov, Justin: make it visible).
        pending = [i for i in ids if i not in _fleet_ids()]
        if pending:
            checks.append({"key": "sync", "label": "Not in the fleet view yet", "state": "warn",
                           "detail": ((f"{len(pending)} of the {n} targets aren't" if n > 1 else "The target isn't")
                                      + " in SSM Explorer yet, so Patchy read "
                                      + ("it" if len(pending) == 1 else "them")
                                      + " from EC2: it has the tags but no patch state. The run includes "
                                      + ("it" if len(pending) == 1 else "them") + "; the Where list marks "
                                      + ("it." if len(pending) == 1 else "which.")),
                           "pending": len(pending)})
        alarms = list(params.get("abort_alarms") if params.get("abort_alarms") is not None
                      else change_controls.abort_alarms())
        checks.append(_alarm_check(alarms, locations))
        sections.append({"key": "stops", "label": "Stops if",
                         "text": (f"{', '.join(alarms)} goes into ALARM in any target account and Region, or you "
                                  "press Stop run. Instances already patching finish.")
                         if alarms else "You press Stop run. Instances already patching finish."})
        sections.append({"key": "undo", "label": "Undo",
                         "text": "Patchy can't undo a rollback; patch again to reinstall."
                         if kind == "rollback" else
                         "Roll back undoes this run's yum transactions on each instance."})
    view = {"kind": kind, "headline": headline, "summary": summary, "environment": env,
            "checks": checks, "sections": sections, "locations": locations,
            "targets": _target_rows(ids), "targets_total": n}
    if (execution_plan.get("coverage") or {}).get("rows"):
        # Schedules by location: which targets a window or patch policy would patch before the deadline.
        view["coverage"] = execution_plan["coverage"]
    return view
