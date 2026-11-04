"""Whether each target instance is already scheduled to be patched before its SLA deadline.

Two kinds of schedule count: a maintenance window in the instance's own account
and Region (as get_maintenance_windows reports it), and a Quick Setup patch
policy visible from the hub, either the hub's own or an organization policy
(the delegated administrator can read those; only the management account can
create them). Spoke-local policies aren't visible from the hub and don't count.

The check is conservative: a plan waits only when every target is proven
covered. A schedule covers an instance when all of these hold:

- it installs (a window with an AWS-RunPatchBaseline Install task, or a policy
  whose operation is ScanAndInstall), and is enabled or deployed;
- it targets the instance: a window's targets match it; a policy names its
  account or an OU above it (nested OUs count), its Region, and its tag or all
  instances;
- its next run finishes before the deadline (a window's duration; a policy is
  given POLICY_RUN_HOURS);
- it reboots when the fix needs one: a NoReboot schedule covers only when this
  plan wouldn't reboot either, or the CVE's packages are known and none is a
  kernel;
- for a CVE, the baseline approves the fix: the instance's last patch scan lists
  a missing patch for that CVE.

Anything that can't be read or proven leaves the instance uncovered, and the
plan patches now. Reads are cached for a few minutes.
"""

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from helper.concurrency import ContextThreadPoolExecutor as ThreadPoolExecutor
from helper.timeutil import as_utc

logger = logging.getLogger(__name__)

POLICY_CACHE_SECONDS = 300
# A policy's install has no duration; assume it takes this long to reach every instance.
POLICY_RUN_HOURS = 2
# Above this many instances, the per-instance approval check isn't attempted.
APPROVAL_CHECK_MAX = 100
_WORKERS = 8
_KERNEL_PREFIXES = ("kernel", "linux-image", "linux-modules", "linux-headers")

_policy_cache: Tuple[float, Optional[List[dict]]] = (0.0, None)
_policy_lock = threading.Lock()
_parents_cache: Dict[str, Tuple[float, Optional[List[str]]]] = {}
_parents_lock = threading.Lock()


# ── Quick Setup patch policies ───────────────────────────────────────

def _split(value) -> List[str]:
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def _deployed(summaries: Iterable[dict]) -> bool:
    """The deployment succeeded, and no async execution failed or is still running.

    A failure anywhere can't be placed, so it disqualifies the policy. Async
    executions read NONE when a deployment needed none, which is fine.
    """
    status = {s.get("StatusType"): s.get("Status") for s in summaries or []}
    return status.get("Deployment") == "SUCCEEDED" and status.get("AsyncExecutions", "NONE") in ("SUCCEEDED", "NONE")


def _policy(manager: dict, definition: dict) -> dict:
    p = definition.get("Parameters") or {}
    return {
        "name": manager.get("Name") or p.get("PatchPolicyName") or manager.get("ManagerArn", "").rsplit("/", 1)[-1],
        "arn": manager.get("ManagerArn"),
        "organization": bool(_split(p.get("TargetOrganizationalUnits"))),
        "installs": p.get("ConfigurationOptionsPatchOperation") == "ScanAndInstall",
        "schedule": p.get("ConfigurationOptionsInstallValue") or "",
        "reboot": p.get("RebootOption") or "RebootIfNeeded",
        "ous": _split(p.get("TargetOrganizationalUnits")),
        "accounts": _split(p.get("TargetAccounts")),
        "regions": _split(p.get("TargetRegions")),
        "target_type": p.get("TargetType") or "*",
        "tag_key": p.get("TargetTagKey"),
        "tag_value": p.get("TargetTagValue"),
        "deployed": _deployed(manager.get("StatusSummaries")),
    }


class _Policies(list):
    """Patch policies read, and the ones listed that couldn't be opened: [{"name", "region", "error"}]."""
    unreadable: List[dict] = []


def _read_policies(regions: List[str]) -> Optional[List[dict]]:
    """Patch policies visible from the hub, in each Region a policy could live in; None if none could be listed.

    Each policy is opened on its own, so one that can't be (30 Oct live: Unauthorized on a new organization
    policy) is reported as unreadable rather than hiding the rest, or passing for "no schedule".
    """
    from helper.cross_account import get_client
    found: Dict[str, dict] = {}
    unreadable: List[dict] = []
    read_any = False
    for region in regions:
        try:
            qs = get_client("ssm-quicksetup", region=region)
            token = None
            while True:
                page = qs.list_configuration_managers(**({"StartingToken": token} if token else {}))
                read_any = True
                for summary in page.get("ConfigurationManagersList") or []:
                    if summary.get("ManagerArn") in found or not any(
                            d.get("Type") == "AWSQuickSetupType-PatchPolicy"
                            for d in summary.get("ConfigurationDefinitionSummaries") or []):
                        continue
                    try:
                        manager = qs.get_configuration_manager(ManagerArn=summary["ManagerArn"])
                    except Exception as e:
                        name = summary.get("Name") or summary["ManagerArn"].rsplit("/", 1)[-1]
                        logger.warning(f"[COVERAGE] Quick Setup policy {name} in {region} couldn't be read: {e}")
                        unreadable.append({"name": name, "region": region, "error": str(e)[:200]})
                        continue
                    manager.setdefault("StatusSummaries", summary.get("StatusSummaries"))
                    for d in manager.get("ConfigurationDefinitions") or []:
                        if d.get("Type") == "AWSQuickSetupType-PatchPolicy":
                            found[f"{summary['ManagerArn']}#{d.get('Id')}"] = _policy(manager, d)
                token = page.get("NextToken")
                if not token:
                    break
        except Exception as e:
            logger.warning(f"[COVERAGE] Quick Setup policies in {region} couldn't be listed: {e}")
            unreadable.append({"name": f"policies in {region}", "region": region, "error": str(e)[:200]})
    if not read_any:
        return None
    out = _Policies(found.values())
    out.unreadable = unreadable
    return out


def patch_policies() -> Optional[List[dict]]:
    """Quick Setup patch policies, cached; None when they couldn't be read (nothing counts as covered by one)."""
    global _policy_cache
    from helper.tools._shared import AWS_REGION, SPOKE_REGIONS
    with _policy_lock:
        at, policies = _policy_cache
        if policies is not None and time.time() - at < POLICY_CACHE_SECONDS:
            return policies
        policies = _read_policies(list(dict.fromkeys([AWS_REGION] + list(SPOKE_REGIONS))))
        if policies is not None:
            _policy_cache = (time.time(), policies)
        return policies


def _ancestors(account_id: str) -> Optional[List[str]]:
    """The OUs above an account and the root, nearest first (cached); None if Organizations can't say."""
    with _parents_lock:
        at, chain = _parents_cache.get(account_id, (0.0, None))
        if chain is not None and time.time() - at < POLICY_CACHE_SECONDS:
            return chain
    from helper.cross_account import get_client
    try:
        org = get_client("organizations", region="us-east-1")
        chain, child = [], account_id
        for _ in range(6):  # Organizations allows five OU levels under the root
            parents = org.list_parents(ChildId=child).get("Parents") or []
            if not parents:
                break
            child = parents[0]["Id"]
            chain.append(child)
            if parents[0].get("Type") == "ROOT":
                break
    except Exception as e:
        logger.warning(f"[COVERAGE] parents of {account_id} couldn't be read: {e}")
        return None
    with _parents_lock:
        _parents_cache[account_id] = (time.time(), chain)
    return chain


def _policy_targets(policy: dict, account: str, region: str, tags: Optional[Dict[str, str]]) -> Optional[str]:
    """None when the policy targets this instance, else why not."""
    if region not in policy["regions"]:
        return "not this Region"
    if account not in policy["accounts"]:
        if not policy["ous"]:
            return "not this account"
        chain = _ancestors(account)
        if chain is None:
            return "this account's OU couldn't be read"
        if not set(chain) & set(policy["ous"]):
            return "not this account's OU"
    if policy["target_type"] in ("*", "All", "all"):
        return None
    if policy["target_type"] == "Tags":
        if tags is None:
            return "the instance's tags couldn't be read"
        return None if tags.get(policy["tag_key"] or "") == policy["tag_value"] else "not this tag"
    return f"targets by {policy['target_type']}, which Patchy can't check"


# ── the per-instance check ───────────────────────────────────────────

def _needs_reboot(packages: Optional[List[str]]) -> bool:
    """Whether the fix needs a reboot to take effect; unknown packages are assumed to."""
    if not packages:
        return True
    return any(str(p).lower().startswith(_KERNEL_PREFIXES) for p in packages)


def _approved_fixes(ids_by_loc: Dict[Tuple[str, str], List[str]], cve_ids: List[str]) -> Dict[str, Optional[bool]]:
    """Per instance: its last scan lists a missing patch for one of these CVEs (the baseline approves the fix).
    None when the patch state couldn't be read, which says nothing about the baseline (2 Nov review, M11)."""
    from helper.retry import throttled
    from helper.cross_account import get_client
    wanted = {c.upper() for c in cve_ids}

    def check(item):
        (account, region), iid = item
        try:
            ssm = get_client("ssm", account_id=account, region=region)
            pages = throttled(lambda: list(ssm.get_paginator("describe_instance_patches").paginate(
                InstanceId=iid, Filters=[{"Key": "State", "Values": ["Missing"]}])))
            for page in pages:
                for patch in page.get("Patches") or []:
                    if wanted & {c.strip().upper() for c in str(patch.get("CVEIds") or "").split(",")}:
                        return iid, True
            return iid, False
        except Exception as e:
            logger.warning(f"[COVERAGE] patch state of {iid} couldn't be read: {e}")
            return iid, None

    items = [(loc, iid) for loc, ids in ids_by_loc.items() for iid in ids]
    if not items:
        return {}
    with ThreadPoolExecutor(max_workers=min(_WORKERS, len(items))) as pool:
        return dict(pool.map(check, items))


def _when(dt: datetime) -> str:
    return dt.strftime("%d %b %H:%M UTC")


def check(environment: str, instance_ids: List[str], deadline: datetime, *, plan_reboots: bool,
          cve_ids: Optional[List[str]] = None, packages: Optional[List[str]] = None,
          now: Optional[datetime] = None) -> dict:
    """Coverage of each target before the deadline.

    Returns:
        {all_covered, covered, uncovered, deadline, rows, windows_skipped, latest}: rows are one per
        account and Region, {account, region, instances, covered, schedule, before_deadline, verdict};
        latest is the covering run that finishes last, when every target is covered.
    """
    from helper.tools import _shared, get_maintenance_windows
    now = now or datetime.now(timezone.utc)
    cve_ids = [c for c in (cve_ids or []) if c and c != "N/A"]
    reboot_needed = _needs_reboot(packages)
    ids = list(dict.fromkeys(instance_ids))
    _shared._get_fleet_summary()
    fleet = _shared.fleet_instances()
    ids_by_loc: Dict[Tuple[str, str], List[str]] = {}
    unlocated = []
    for iid in ids:
        info = fleet.get(iid) or {}
        if info.get("account_id") and info.get("region"):
            ids_by_loc.setdefault((info["account_id"], info["region"]), []).append(iid)
        else:
            unlocated.append(iid)
    for loc, group in (_shared._locate_instances(unlocated).items() if unlocated else []):
        ids_by_loc.setdefault(loc, []).extend(group)

    # Each instance's runs that would patch it: (finishes at, label, problem or None).
    runs: Dict[str, List[Tuple[datetime, str, Optional[str]]]] = {iid: [] for iid in ids}
    skipped: List[Dict[str, str]] = []

    def reboot_problem(option: str) -> Optional[str]:
        if option == "NoReboot" and plan_reboots and reboot_needed:
            return "installs without a reboot; this fix needs one"
        return None

    found_windows = get_maintenance_windows(environment=environment, instance_ids=ids)
    # Windows that couldn't be read might cover some targets; they don't count, and the plan says so.
    windows_unreadable = [f"{u['account_id']}/{u['region']}" + (f" {u['window_id']}" if u.get('window_id') else "")
                          for u in found_windows.get("unreadable") or []]
    if found_windows.get("error") and not found_windows.get("windows"):
        windows_unreadable.append("every location")
    for where in windows_unreadable:
        skipped.append({"name": f"maintenance windows in {where}", "reason": "couldn't be read, so they don't count"})
    for w in found_windows.get("windows") or []:
        name = w.get("name", "unknown")
        if not w.get("enabled", True):
            skipped.append({"name": name, "reason": "is disabled"})
            continue
        if not w.get("has_patch_task"):
            skipped.append({"name": name, "reason": "has no patch install task"})
            continue
        start = as_utc(w.get("next_execution_utc"))
        if start is None or start < now:
            skipped.append({"name": name, "reason": "has no upcoming run"})
            continue
        end = start + timedelta(hours=float(w.get("duration_hours") or 0))
        problem = reboot_problem(w.get("reboot_option") or "RebootIfNeeded")
        if end > deadline:
            skipped.append({"name": name, "reason": f"finishes after the SLA deadline ({_when(deadline)})"})
        elif problem:
            skipped.append({"name": name, "reason": problem})
        for iid in w.get("instances_covered") or []:
            if iid in runs:
                runs[iid].append((end, f"Window {name}, {_when(start)}", problem))

    policies = patch_policies()
    # A policy that couldn't be opened might cover some targets; nothing counts as covered by it, and the plan says so.
    for bad in getattr(policies, "unreadable", None) or []:
        skipped.append({"name": bad["name"], "reason": "couldn't be read, so it doesn't count"})
    tags = _shared._instance_tags_by_id(ids) if policies and any(
        p["target_type"] == "Tags" for p in policies) else {}
    from helper.forecast import aws_cron_next
    for policy in policies or []:
        label = f"{'Organization' if policy['organization'] else 'Quick Setup'} policy {policy['name']}"
        if not policy["installs"]:
            continue  # scan-only: it reports, it doesn't patch
        if not policy["deployed"]:
            skipped.append({"name": policy["name"], "reason": "isn't deployed everywhere it targets"})
            continue
        start = aws_cron_next(policy["schedule"], now)
        if start is None:
            skipped.append({"name": policy["name"], "reason": "has a schedule Patchy can't read"})
            continue
        end = start + timedelta(hours=POLICY_RUN_HOURS)
        problem = reboot_problem(policy["reboot"])
        for (account, region), group in ids_by_loc.items():
            for iid in group:
                if _policy_targets(policy, account, region, tags.get(iid) if tags else None) is None:
                    runs[iid].append((end, f"{label}, {_when(start)}", problem))

    # A CVE fix counts only where the baseline approves it.
    approved: Dict[str, bool] = {}
    not_approved = "the last patch scan doesn't list this CVE's fix as approved and missing"
    if cve_ids:
        pending = {loc: [i for i in group if any(e <= deadline and not p for e, _, p in runs[i])]
                   for loc, group in ids_by_loc.items()}
        if sum(len(g) for g in pending.values()) <= APPROVAL_CHECK_MAX:
            approved = _approved_fixes(pending, cve_ids)
        else:
            not_approved = f"more than {APPROVAL_CHECK_MAX} instances, too many to check the baseline approves the fix"

    rows, covered, uncovered, finishes = [], [], [], []
    for (account, region), group in sorted(ids_by_loc.items()):
        loc_covered, schedules, reasons, latest = 0, set(), set(), None
        for iid in group:
            good = sorted((e, label) for e, label, p in runs[iid] if e <= deadline and not p)
            if good and cve_ids and not approved.get(iid):
                reasons.add("its patch state couldn't be read" if approved.get(iid) is None and iid in approved
                            else not_approved)
                good = []
            if good:
                loc_covered += 1
                covered.append(iid)
                schedules.add(good[0][1])
                latest = max(latest or good[0][0], good[0][0])
                finishes.append((good[0][0], good[0][1]))
            else:
                uncovered.append(iid)
                late = [label for e, label, p in runs[iid] if e > deadline]
                bad = [p for e, label, p in runs[iid] if p]
                reasons.add(bad[0] if bad else "next run finishes after the deadline" if late else "no patch schedule")
        rows.append({
            "account": account, "region": region, "instances": len(group), "covered": loc_covered,
            "schedule": "; ".join(sorted(schedules)) or "None that qualifies",
            "before_deadline": (f"Yes, {round((deadline - latest).total_seconds() / 3600)} h margin"
                                if latest and loc_covered == len(group) else "No"),
            "verdict": ("Covered" if loc_covered == len(group) else
                        f"Not covered: {'; '.join(sorted(reasons))}" if not loc_covered else
                        f"{loc_covered} of {len(group)} covered: {'; '.join(sorted(reasons))}"),
        })
    all_covered = bool(ids) and not uncovered
    last = max(finishes) if all_covered and finishes else None
    return {"all_covered": all_covered, "covered": covered, "uncovered": uncovered,
            "deadline": deadline.isoformat(), "rows": rows, "windows_skipped": skipped,
            "latest": {"finishes_at": last[0].isoformat(), "schedule": last[1]} if last else None,
            "policies_read": policies is not None,
            "policies_unreadable": [p["name"] for p in getattr(policies, "unreadable", None) or []],
            "windows_unreadable": windows_unreadable}


def table_text(result: dict, sla_hours, sla_source) -> str:
    """One line for the plan's SLA check."""
    n = len(result["covered"]) + len(result["uncovered"])
    deadline = as_utc(result["deadline"])
    base = f"{len(result['uncovered'])} of {n} instances aren't scheduled to be patched before the " \
           f"{sla_hours} h SLA deadline ({sla_source or 'default'}, {_when(deadline)})"
    if not result.get("policies_read"):
        base += "; Quick Setup patch policies couldn't be read, so none counted"
    elif result.get("policies_unreadable"):
        base += f"; {', '.join(result['policies_unreadable'][:3])} couldn't be read, so didn't count"
    if result.get("windows_unreadable"):
        base += f"; maintenance windows in {', '.join(result['windows_unreadable'][:3])} couldn't be read, so didn't count"
    return base + ", so the plan patches every target now. Cancel it to wait, and those instances breach."

