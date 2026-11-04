"""Verify a CVE fix by whether Inspector closed the finding, and say why if not.

Each instance is checked in its own account and region. An instance with no
finding for the CVE is reported as such, never counted as fixed: Inspector may
not have scanned it. A terminated instance is reported as terminated, because
Inspector closes its findings when it goes. An open finding gets a cause from the instance's own patch
records for the CVE's packages:

- pending_reboot: the fixed package is installed but the instance hasn't rebooted
- awaiting_rescan: the fix was installed after Inspector last looked
- fix_not_installed: the package wasn't updated since Inspector last saw it
- no_fix_available: Inspector reports no fix
"""

import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from helper.cross_account import get_client
from helper.retry import throttled
from helper.timeutil import as_utc

_CAUSE_TEXT = {
    "pending_reboot": "installed, waiting for a reboot",
    "awaiting_rescan": "fix installed, Inspector hasn't rescanned yet",
    "fix_not_installed": "fix not installed",
    "no_fix_available": "no fix available yet",
}


def _locate(instance_ids: List[str]) -> Dict[tuple, List[str]]:
    from helper.tools._shared import _locate_instances
    return _locate_instances(instance_ids)


_ts = as_utc

def _iso(value) -> Optional[str]:
    t = _ts(value)
    return t.isoformat() if t else None


def _package_records(packages: List[dict], patches: List[dict]) -> List[dict]:
    """Patch records for the CVE's packages ("name.arch:..." titles, arch lower-cased as RPM writes it)."""
    prefixes = []
    for p in packages:
        name, arch = p.get("name") or "", (p.get("arch") or "").lower()
        if name:
            prefixes.append(f"{name}.{arch}:" if arch else f"{name}.")
    return [r for r in patches if any(str(r.get("Title", "")).startswith(px) for px in prefixes)]


_INSTALLED = ("Installed", "InstalledOther", "InstalledPendingReboot")


def _fixed_records(packages: List[dict], records: List[dict]) -> Optional[List[dict]]:
    """Patch records at or above the fixed version ("name.arch:<version>"), or None if Inspector gave none."""
    from helper import rpmver
    wanted = []
    for p in packages:
        name, arch, fixed = p.get("name") or "", (p.get("arch") or "").lower(), p.get("fixedInVersion")
        if not name or not fixed or fixed == "NotAvailable":
            continue
        wanted.append((f"{name}.{arch}" if arch else f"{name}.", fixed))
    if not wanted:
        return None
    # Every package Inspector names must be at its own fixed version: a CVE in openssl and
    # openssl-libs isn't fixed by updating openssl alone (2 Nov review, M6). Empty when any isn't.
    out = []
    for name_arch, fixed in dict(wanted).items():
        match = None
        for r in records:
            # A Missing or Failed record names the build available, not one installed.
            if r.get("State") not in _INSTALLED:
                continue
            version = rpmver.installed_version(str(r.get("Title", "")), name_arch)
            if version and rpmver.at_least(version, fixed):
                match = r
                break
        if match is None:
            return []
        out.append(match)
    return out


def classify(cve_id: str, instance_id: str, findings: List[dict], patches: List[dict]) -> dict:
    """Verification result for one CVE on one instance.

    Args:
        findings: Inspector findings for this CVE on this instance, any status.
        patches: The instance's DescribeInstancePatches records.
    """
    result = {"cve_id": cve_id, "instance_id": instance_id}
    if not findings:
        return {**result, "status": "no_finding"}
    closed = [f for f in findings if f.get("status") == "CLOSED"]
    active = [f for f in findings if f.get("status") == "ACTIVE"]
    packages = [p for f in findings for p in (f.get("packageVulnerabilityDetails") or {}).get("vulnerablePackages") or []]
    records = _package_records(packages, patches)
    installs = [_ts(r.get("InstalledTime")) for r in records if _ts(r.get("InstalledTime"))]
    patched_at = max(installs).isoformat() if installs else None
    if closed and not any(_ts(a.get("lastObservedAt")) and _ts(c.get("updatedAt")) and
                          _ts(a["lastObservedAt"]) > _ts(c["updatedAt"]) for a in active for c in closed):
        latest = max(closed, key=lambda f: _ts(f.get("updatedAt")) or datetime.min.replace(tzinfo=timezone.utc))
        return {**result, "status": "fixed", "verified_at": _iso(latest.get("updatedAt")), "patched_at": patched_at}
    current = max(active, key=lambda f: _ts(f.get("lastObservedAt")) or datetime.min.replace(tzinfo=timezone.utc))
    last_seen = _ts(current.get("lastObservedAt"))
    fixed = _fixed_records(packages, records)
    if current.get("fixAvailable") == "NO":
        cause = "no_fix_available"
    elif fixed is not None:
        # Inspector names the fixed version: decide from what's installed, not from timings.
        if not fixed:
            cause = "fix_not_installed"
        elif any(r.get("State") == "InstalledPendingReboot" for r in fixed):
            cause = "pending_reboot"
        else:
            cause = "awaiting_rescan"
    elif any(r.get("State") == "InstalledPendingReboot" for r in records):
        cause = "pending_reboot"
    elif installs and last_seen and max(installs) > last_seen:
        cause = "awaiting_rescan"
    else:
        cause = "fix_not_installed"
    return {**result, "status": "open", "cause": cause, "last_observed_at": _iso(last_seen), "patched_at": patched_at}


def _findings(inspector, cve_id: str, instance_id: str) -> List[dict]:
    out = []
    criteria = {"vulnerabilityId": [{"comparison": "EQUALS", "value": cve_id}],
                "resourceId": [{"comparison": "EQUALS", "value": instance_id}]}
    for page in inspector.get_paginator("list_findings").paginate(filterCriteria=criteria):
        out.extend(page.get("findings") or [])
    return out


def _patches(ssm, instance_id: str) -> List[dict]:
    out = []
    for page in ssm.get_paginator("describe_instance_patches").paginate(InstanceId=instance_id):
        out.extend(page.get("Patches") or [])
    return out


def _gone(ec2, ids: List[str]) -> Tuple[set, set]:
    """(terminated, not listed): instances EC2 reports terminated or shutting down, and ones it doesn't
    list here at all. Inspector closes a terminated instance's findings, which isn't a fix. One not
    listed may have been terminated a while ago, or be in another location: it's unlocated, not
    terminated (2 Nov review, M9)."""
    state = {}
    for n in range(0, len(ids), 200):  # the filter takes up to 200 values
        for page in ec2.get_paginator("describe_instances").paginate(
                Filters=[{"Name": "instance-id", "Values": ids[n:n + 200]}]):
            for r in page.get("Reservations") or []:
                for i in r.get("Instances") or []:
                    state[i["InstanceId"]] = (i.get("State") or {}).get("Name")
    return ({i for i in ids if state.get(i) in ("shutting-down", "terminated")},
            {i for i in ids if i not in state})


def _throttled(read):
    """read(), retried while AWS throttles it (2 Nov: one of five showed "Couldn't check")."""
    return throttled(read, sleep=lambda s: time.sleep(s))


def verify(cve_id: str, instance_ids: List[str]) -> dict:
    """Check one CVE on these instances, each in its own account and region.

    Inspector findings are read with hub credentials per region (a delegated
    administrator sees the organisation's findings); patch records are read in
    the instance's account.
    """
    rows = []
    for (account_id, region), ids in _locate(instance_ids).items():
        try:
            inspector = get_client("inspector2", region=region)
            ssm = get_client("ssm", account_id=account_id, region=region)
        except Exception as e:  # one location's role failing shouldn't lose every row (2 Nov review, L4)
            rows += [{"cve_id": cve_id, "instance_id": i, "status": "unknown", "error": str(e)[:200]} for i in ids]
            continue
        try:
            gone, unlisted = _gone(get_client("ec2", account_id=account_id, region=region), ids)
        except Exception:  # without the instance state, judge by the findings as before
            gone, unlisted = set(), set()
        for iid in ids:
            if iid in gone:
                rows.append({"cve_id": cve_id, "instance_id": iid, "status": "terminated"})
                continue
            if iid in unlisted:
                rows.append({"cve_id": cve_id, "instance_id": iid, "status": "unlocated",
                             "where": f"{account_id}/{region}"})
                continue
            try:
                rows.append(classify(cve_id, iid, _throttled(lambda: _findings(inspector, cve_id, iid)),
                                     _throttled(lambda: _patches(ssm, iid))))
            except Exception as e:  # one unreadable instance shouldn't hide the rest
                rows.append({"cve_id": cve_id, "instance_id": iid, "status": "unknown", "error": str(e)[:200]})
    counts = {s: sum(1 for r in rows if r["status"] == s)
              for s in ("fixed", "open", "no_finding", "terminated", "unlocated", "unknown")}
    return {"cve_id": cve_id, "instances": rows, "counts": counts}


def _ago(iso: Optional[str], now: datetime) -> str:
    t = _ts(iso)
    if not t:
        return ""
    hours = (now - t).total_seconds() / 3600
    return "just now" if hours < 1 else f"{int(hours)} h ago" if hours < 48 else f"{int(hours // 24)} d ago"


def verification_card(result: dict, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    rows = []
    for r in result["instances"][:12]:
        if r["status"] == "fixed":
            ago = _ago(r.get("verified_at"), now)
            rows.append({"label": r["instance_id"], "status": f"Fixed, verified {ago}".strip(", "), "tone": "ok"})
        elif r["status"] == "open":
            rows.append({"label": r["instance_id"], "status": f"Open: {_CAUSE_TEXT.get(r.get('cause'), 'unknown cause')}",
                         "tone": "warn" if r.get("cause") in ("pending_reboot", "awaiting_rescan") else "bad"})
        elif r["status"] == "no_finding":
            rows.append({"label": r["instance_id"], "status": "No Inspector finding (not affected or not yet scanned)"})
        elif r["status"] == "terminated":
            rows.append({"label": r["instance_id"], "status": "Terminated, so not checked"})
        elif r["status"] == "unlocated":
            rows.append({"label": r["instance_id"], "status": "Couldn't locate; it may have been terminated",
                         "tone": "warn"})
        else:
            rows.append({"label": r["instance_id"], "status": "Couldn't check", "tone": "bad"})
    total = sum(1 for r in result["instances"] if r["status"] != "terminated")
    return {"type": "verification", "title": f"{result['cve_id']} · fixed on {result['counts']['fixed']} of {total}",
            "subtitle": "Inspector finding status per instance", "rows": rows}
