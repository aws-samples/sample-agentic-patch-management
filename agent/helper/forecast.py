"""Forecast which findings scheduled patching will miss before their SLA.

For each finding on each instance:

- the deadline is Inspector's first observation plus the SLA hours for its severity;
- the next scheduled install is the earliest maintenance window with a patch
  task covering the instance before the deadline, or the next run of a Quick
  Setup patch policy Install association;
- the install includes the fix only if the fixed package is Missing after the
  last scan (the baseline approves it). Installed at the fixed version means
  it's already fixed; neither means the baseline won't install it yet.
"""

import calendar
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from helper.timeutil import as_utc

logger = logging.getLogger(__name__)

_DOW = {"SUN": 1, "MON": 2, "TUE": 3, "WED": 4, "THU": 5, "FRI": 6, "SAT": 7}
_MON = {m.upper(): i for i, m in enumerate(calendar.month_abbr) if m}
_HORIZON_DAYS = 400


# ── AWS cron ─────────────────────────────────────────────────────────

def _field(spec: str, low: int, high: int, names: Optional[dict] = None) -> Optional[set]:
    """Values an AWS cron field allows; None for '*' or '?'. Raises ValueError on forms not supported."""
    if spec in ("*", "?"):
        return None
    values = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", ""):
            a, b = low, high
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = (names or {}).get(x.upper(), None) or int(x), (names or {}).get(y.upper(), None) or int(y)
        else:
            a = b = (names or {}).get(part.upper(), None) or int(part)
        if not (low <= a <= high and low <= b <= high):
            raise ValueError(spec)
        values.update(range(a, b + 1, step))
    return values


def aws_cron_next(expression: str, after: datetime) -> Optional[datetime]:
    """Next time an AWS cron expression fires after `after` (UTC), or None if it can't be read.

    Supports numbers, names, lists, ranges, steps, '*' and '?'. Forms like L, W
    and # return None rather than a guess.
    """
    expr = (expression or "").strip()
    if not (expr.startswith("cron(") and expr.endswith(")")):
        return None
    parts = expr[5:-1].split()
    if len(parts) != 6:
        return None
    try:
        minutes = _field(parts[0], 0, 59)
        hours = _field(parts[1], 0, 23)
        doms = _field(parts[2], 1, 31)
        months = _field(parts[3], 1, 12, _MON)
        dows = _field(parts[4], 1, 7, _DOW)
        years = _field(parts[5], 1970, 2199)
    except ValueError:
        return None
    t = (after.astimezone(timezone.utc) + timedelta(minutes=1)).replace(second=0, microsecond=0)
    end = t + timedelta(days=_HORIZON_DAYS)
    while t < end:
        day_ok = (doms is None or t.day in doms) and (dows is None or (t.isoweekday() % 7) + 1 in dows)
        if (years is None or t.year in years) and (months is None or t.month in months) and day_ok:
            if hours is None or t.hour in hours:
                if minutes is None or t.minute in minutes:
                    return t
                t += timedelta(minutes=1)
                continue
            t = (t + timedelta(hours=1)).replace(minute=0)
            continue
        t = (t + timedelta(days=1)).replace(hour=0, minute=0)
    return None


# ── verdict ──────────────────────────────────────────────────────────

def _titles(packages: List[dict]) -> Tuple[List[str], List[str]]:
    """Patch record titles at the fixed version, and 'name.arch:' prefixes for the package."""
    fixed, prefixes = [], []
    for p in packages:
        name, arch = p.get("name") or "", (p.get("arch") or "").lower()
        if not name:
            continue
        prefixes.append(f"{name}.{arch}:" if arch else f"{name}.")
        # fixed_in in _find_vulnerabilities' findings, fixedInVersion in Inspector's own.
        version = p.get("fixed_in") or p.get("fixedInVersion")
        if version and version != "NotAvailable":
            fixed.append(f"{name}.{arch}:{version}" if arch else f"{name}.:{version}")
    return fixed, prefixes


def _at_or_above(record: dict, fixed: List[str]) -> bool:
    """Whether a patch record's version is at or above one of the fixed 'name.arch:version' titles."""
    from helper import rpmver
    title = str(record.get("Title", ""))
    for f in fixed:
        name_arch, _, version = f.partition(":")
        installed = rpmver.installed_version(title, name_arch)
        if installed and rpmver.at_least(installed, version):
            return True
    return False


def verdict(packages: List[dict], patches: List[dict], deadline: Optional[datetime], now: datetime,
            next_install: Optional[datetime], window_ok: bool) -> Tuple[str, str]:
    """(verdict, why) for one finding on one instance."""
    if patches is None:
        return "unknown", "the instance's patch state couldn't be read"
    if not patches:
        return "unknown", "no patch scan for this instance"
    fixed, prefixes = _titles(packages)
    mine = [r for r in patches if any(str(r.get("Title", "")).startswith(px) for px in prefixes)]
    installed = [r for r in mine if r.get("State") in ("Installed", "InstalledPendingReboot", "InstalledOther")]
    # Fixed only when every package has its fix, as the fix check decides (2 Nov review, M6).
    if fixed and all(any(_at_or_above(r, [f]) for r in installed) for f in dict.fromkeys(fixed)):
        return "fixed", "fix installed; reboot or rescan pending"
    if deadline is None:
        return "unknown", "no SLA for this severity"
    if deadline <= now:
        return "overdue", "SLA deadline has passed"
    if not any(r.get("State") == "Missing" for r in mine):
        return "will_miss", "patch baseline does not approve the fix yet"
    if window_ok:
        return "on_track", "a maintenance window installs it before the deadline"
    if next_install and next_install <= deadline:
        return "on_track", "patch policy installs it before the deadline"
    if next_install:
        return "will_miss", "next scheduled install is after the deadline"
    return "will_miss", "no scheduled install covers it"


# ── data (thin wrappers over AWS calls) ───────────────────────────

def _environment_instances(environment: str) -> List[str]:
    from helper.tools.patch_tools import _environment_instance_ids
    return _environment_instance_ids(environment, [])


# The forecast assesses at most this many findings, most severe first.
FORECAST_FINDINGS_MAX = 2000


class _Findings(list):
    """Findings, whether every Region could be read (complete), and whether the cap cut the list (cut_off)."""
    complete = True
    cut_off = False


def _findings(environment: str) -> List[dict]:
    from helper.tools.vulnerability_tools import _find_vulnerabilities
    result = _find_vulnerabilities(environment=environment, limit=FORECAST_FINDINGS_MAX)
    out = _Findings(result.get("findings") or [])
    out.complete = not (result.get("partial_failures") or result.get("error") or result.get("scope_unresolved"))
    out.cut_off = bool(result.get("truncated"))
    return out


def _patch_records(instance_id: str) -> Optional[List[dict]]:
    """The instance's patch records; None when they couldn't be read (2 Nov review, L1)."""
    from helper import verification
    from helper.cross_account import get_client
    try:
        for (account, region), ids in verification._locate([instance_id]).items():
            ssm = get_client("ssm", account_id=account, region=region)
            return verification._throttled(lambda: verification._patches(ssm, instance_id))
    except Exception as e:
        logger.warning(f"[FORECAST] patch records for {instance_id} couldn't be read: {e}")
        return None
    return []


# SLA hours per (instance, severity), refreshed so a changed SLA tag counts within minutes.
SLA_CACHE_SECONDS = 600
SLA_CACHE_MAX = 20000
_sla_cache: Dict[tuple, Tuple[float, Optional[int]]] = {}


def _sla_hours_each(instance_ids: List[str], severity: str) -> Dict[str, Optional[int]]:
    """SLA hours for each instance at this severity, with one tag read for all the uncached ones."""
    now = time.time()
    out: Dict[str, Optional[int]] = {}
    missing = []
    for iid in instance_ids:
        cached = _sla_cache.get((iid, severity))
        if cached and now - cached[0] < SLA_CACHE_SECONDS:
            out[iid] = cached[1]
        else:
            missing.append(iid)
    if missing:
        from helper.tools._shared import _sla_hours_each as read
        fresh = read(missing, severity)
        if len(_sla_cache) > SLA_CACHE_MAX:  # instance churn: start over rather than grow without bound
            _sla_cache.clear()
        for iid in missing:
            out[iid] = fresh.get(iid)
            _sla_cache[(iid, severity)] = (now, out[iid])
    return out


def _next_policy_installs(instance_ids: List[str], environment: str, now: datetime) -> Dict[str, Optional[datetime]]:
    """The next patch-policy install for each instance, from one read for all of them."""
    from helper.tools.maintenance_tools import get_patch_policy
    result = getattr(get_patch_policy, "_tool_func", get_patch_policy)(list(instance_ids), environment)
    out: Dict[str, Optional[datetime]] = {}
    for iid, policies in (result.get("instance_policies") or {}).items():
        runs = [aws_cron_next(p.get("schedule") or "", now) for p in policies
                if str(p.get("operation", "")).lower() == "install"]
        runs = [r for r in runs if r]
        out[iid] = min(runs) if runs else None
    return out


class _WindowEnds(dict):
    """Window end times per instance, and whether every location's windows were read."""
    complete = True


def _patch_window_ends_each(environment: str, instance_ids: List[str], now: datetime) -> Dict[str, List[datetime]]:
    """When each upcoming maintenance window that patches an instance ends, per instance.

    Same rules as _window_check (enabled, AWS-RunPatchBaseline task, covers the
    instance), from one read of the environment's windows for all the instances.
    """
    from helper.tools import get_maintenance_windows
    out = _WindowEnds()
    found = get_maintenance_windows(environment=environment, instance_ids=list(instance_ids))
    out.complete = not (found.get("unreadable") or found.get("error"))
    for w in found.get("windows") or []:
        if not w.get("enabled", True) or not w.get("has_patch_task"):
            continue
        start = _ts(w.get("next_execution_utc")) if w.get("next_execution_utc") else None
        if not start or start < now:
            continue
        end = start + timedelta(hours=float(w.get("duration_hours") or 0))
        for iid in w.get("instances_covered") or []:
            out.setdefault(iid, []).append(end)
    return out


_ts = as_utc

# ── forecast ─────────────────────────────────────────────────────────

_ORDER = {"overdue": 0, "will_miss": 1, "unknown": 2, "on_track": 3, "fixed": 4}


def forecast(environment: str, now: Optional[datetime] = None) -> dict:
    """Per CVE in an environment: how many instances will be fixed in time, and which won't."""
    now = now or datetime.now(timezone.utc)
    targets = set(_environment_instances(environment))
    records: Dict[str, List[dict]] = {}
    by_cve: Dict[str, dict] = {}
    findings = _findings(environment)
    # The instances and severities that matter, so schedules and SLA tags are read once each.
    wanted = [f for f in findings if f.get("cve_id") and f.get("packages")
              and any(i in targets for i in (f.get("affected_resources") or []))]
    hit_ids = sorted({i for f in wanted for i in f.get("affected_resources") or [] if i in targets})
    window_ends = _patch_window_ends_each(environment, hit_ids, now) if hit_ids else {}
    next_install = _next_policy_installs(hit_ids, environment, now) if hit_ids else {}
    slas: Dict[str, Dict[str, Optional[int]]] = {}
    for sev in {f.get("severity") or "" for f in wanted}:
        ids = sorted({i for f in wanted if (f.get("severity") or "") == sev
                      for i in f.get("affected_resources") or [] if i in targets})
        slas[sev] = _sla_hours_each(ids, sev)
    for f in findings:
        cve = f.get("cve_id")
        hit = [i for i in (f.get("affected_resources") or []) if i in targets]
        if not cve or not hit or not f.get("packages"):
            continue  # no packages: not a package vulnerability, so patching can't fix it
        group = by_cve.setdefault(cve, {"cve_id": cve, "severity": f.get("severity"), "counts": Counter(),
                                        "instances_at_risk": [], "reasons": Counter(), "deadline": None,
                                        "fix_available": False})
        group["fix_available"] = group["fix_available"] or f.get("fix_available") != "NO"  # YES or PARTIAL
        first = _ts(f.get("first_observed_at"))
        for iid in hit:
            if iid not in records:
                records[iid] = _patch_records(iid)
            sla = slas.get(f.get("severity") or "", {}).get(iid)
            deadline = first + timedelta(hours=sla) if (first and sla) else None
            window_ok = bool(deadline) and any(end <= deadline for end in window_ends.get(iid, []))
            v, why = verdict(f.get("packages") or [], records[iid], deadline, now, next_install.get(iid), window_ok)
            group["counts"][v] += 1
            if v in ("will_miss", "overdue"):
                if iid not in group["instances_at_risk"]:
                    group["instances_at_risk"].append(iid)
                group["reasons"][why] += 1
            if deadline and (group["deadline"] is None or deadline < _ts(group["deadline"])):
                group["deadline"] = deadline.isoformat()
    cves = []
    for g in by_cve.values():
        worst = min((_ORDER[v] for v in g["counts"]), default=4)
        cves.append({**g, "counts": dict(g["counts"]), "reasons": [r for r, _ in g["reasons"].most_common()],
                     "_worst": worst})
    cves.sort(key=lambda g: (g["_worst"], g["deadline"] or "9999"))
    for g in cves:
        g.pop("_worst")
    return {"environment": environment, "cves": cves,
            "will_miss": sum(1 for g in cves if g["counts"].get("will_miss") or g["counts"].get("overdue")),
            "complete": getattr(findings, "complete", True) and getattr(window_ends, "complete", True),
            "cut_off": getattr(findings, "cut_off", False),
            "findings_assessed": len(findings)}


def forecast_card(result: dict) -> dict:
    env, misses = result["environment"], result["will_miss"]
    rows, actions = [], []
    for g in result["cves"][:10]:
        at_risk = len(g["instances_at_risk"])
        if at_risk:
            why = g["reasons"][0] if g["reasons"] else "at risk"
            unknown = g["counts"].get("unknown", 0)
            rows.append({"label": f"{g['cve_id']} · {g.get('severity') or ''}".strip(" ·"),
                         "status": f"{at_risk} at risk: {why}" + (f"; {unknown} not assessed" if unknown else ""),
                         "tone": "bad"})
            if len(actions) < 3 and g.get("fix_available", True):  # nothing to plan where no patch fixes it
                actions.append({"label": f"Plan {g['cve_id']}", "prompt": f"Plan a patch for {g['cve_id']} in {env}"})
        else:
            done = g["counts"].get("fixed", 0) + g["counts"].get("on_track", 0)
            unknown = g["counts"].get("unknown", 0)
            if unknown:
                # Not assessed isn't on track (2 Nov review, M7).
                why = g["reasons"][0] if g.get("reasons") else "couldn't be assessed"
                rows.append({"label": g["cve_id"], "status": f"{done} on track or fixed, {unknown} not assessed"})
            else:
                rows.append({"label": g["cve_id"], "status": f"{done} on track or fixed", "tone": "ok"})
    noun = "CVE" if misses == 1 else "CVEs"
    complete = result.get("complete", True) and not result.get("cut_off")
    unassessed = sum(g["counts"].get("unknown", 0) for g in result["cves"])
    if misses:
        title = f"{env} · {misses} {noun} will miss {'its' if misses == 1 else 'their'} SLA"
        if unassessed:
            # Instances not assessed may miss too (disruption round, 2 Nov).
            title += f"; {unassessed} finding(s) couldn't be assessed"
    elif unassessed:
        title = f"{env} · no misses found; {unassessed} finding(s) couldn't be assessed"
    elif complete:
        title = f"{env} · scheduled patching meets every SLA"
    else:
        title = f"{env} · no misses among the findings read"
    subtitle = "From the last scan, patch schedules and SLA tags"
    if not result.get("complete", True):
        subtitle += ". Incomplete: some findings couldn't be read, so others may also miss"
    elif result.get("cut_off"):
        subtitle += (f". Only the {result.get('findings_assessed')} most severe findings were assessed, "
                     "so others may also miss")
    return {"type": "forecast", "title": title, "subtitle": subtitle, "rows": rows, "actions": actions}
