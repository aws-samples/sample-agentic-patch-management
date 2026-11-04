"""Find the patch run a rollback should undo, and its time window.

Rollback undoes the yum transactions that began during the last finished
Patchy patch run covering the targets (Patchy-YumUndo's Since and Until),
not "the last transaction", which may be a change Patchy didn't make. A run
that failed or was stopped part-way counts: that is when rollback is most
needed. The run comes from its compliance report: start time, duration,
instances, accounts and regions.
"""

import json
import logging
from helper.concurrency import ContextThreadPoolExecutor as ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from helper.timeutil import as_utc

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 30
# Margins around a run: transactions start slightly after the run is
# recorded and the last instance can finish after the parent reports done.
BEFORE_SECONDS = 60
AFTER_SECONDS = 300
# When a report has no duration, the window still ends.
MAX_RUN_SECONDS = 3600
# Runs that ended; one still in progress has no full window yet.
_FINISHED = {"Success", "Failed", "Cancelled", "TimedOut", "CompletedWithFailure", "CompletedWithSuccess"}


def _s3():
    from helper.tools._shared import _s3 as shared_s3
    return shared_s3()


def _bucket() -> str:
    from helper.tools._shared import _get_compliance_bucket_name
    return _get_compliance_bucket_name()


def _now() -> datetime:
    return datetime.now(timezone.utc)


_ts = as_utc
_READ_WORKERS = 10

def _recent_report_keys(now: datetime) -> List[str]:
    s3, bucket = _s3(), _bucket()
    objs = []
    for days in range(LOOKBACK_DAYS + 1):
        prefix = (now - timedelta(days=days)).strftime("%Y/%m/%d/")
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            objs.extend(o for o in page.get("Contents") or [] if o["Key"].endswith(".json"))
    objs.sort(key=lambda o: o["LastModified"], reverse=True)
    return [o["Key"] for o in objs]


_EXECUTION_FIELDS = ("execution_id", "plan_id", "status", "duration_seconds")
_SCOPE_FIELDS = ("environment", "targeting", "instance_ids", "account_ids", "regions")


class ReportsUnreadable(Exception):
    """Some reports in the lookback couldn't be read, so the last run covering the targets isn't known."""


_UNREADABLE = {"_unreadable": True}


def _read_window_fields(s3, bucket: str, key: str) -> Optional[dict]:
    """A report cut down to the fields the window needs, so a month of reports stays small."""
    try:
        report = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except ValueError:
        logger.warning(f"[ROLLBACK_WINDOW] malformed report {key}")
        return None
    except Exception as e:
        # Throttled or denied: this could be the newest run, so the window can't be chosen (2 Nov review, H4).
        logger.warning(f"[ROLLBACK_WINDOW] unreadable report {key}: {e}")
        return _UNREADABLE
    if not isinstance(report, dict):
        return None
    execution, scope = report.get("execution") or {}, report.get("scope") or {}
    return {"operation_type": report.get("operation_type", "patch"), "timestamp": report.get("timestamp"),
            "report_id": report.get("report_id"),
            "execution": {f: execution.get(f) for f in _EXECUTION_FIELDS if f in execution},
            "scope": {f: scope.get(f) for f in _SCOPE_FIELDS if f in scope}}


def last_patch_run(instance_ids: Optional[List[str]] = None, environment: Optional[str] = None,
                   now: Optional[datetime] = None) -> Optional[dict]:
    """The most recent finished patch run covering all these instances (or this environment).

    A plan by instance ID writes one report per account and Region; its reports are
    taken together, so a rollback of the same instances finds the run. An environment
    rollback uses tag-based runs only: an instance-ID run's window says nothing about
    the rest of the environment.

    Returns:
        {execution_id, started_at, status, since, until, account_ids, regions, instance_ids}
        with since and until in epoch seconds, or None when no such run is
        recorded in the lookback window.
    """
    now = now or _now()
    wanted = set(instance_ids or [])
    groups: dict = {}
    s3, bucket = _s3(), _bucket()

    def read(key):
        return _read_window_fields(s3, bucket, key)

    # Every report in the window is read: an instance-ID run writes one per location.
    keys = _recent_report_keys(now)
    with ThreadPoolExecutor(max_workers=_READ_WORKERS) as pool:
        reports = list(pool.map(read, keys))
    unreadable = [k for k, r in zip(keys, reports) if r is _UNREADABLE]
    if unreadable:
        raise ReportsUnreadable(f"{len(unreadable)} report(s) in the last {LOOKBACK_DAYS} days couldn't be read")
    for key, report in zip(keys, reports):
        if report is None:
            continue
        execution, scope = report.get("execution") or {}, report.get("scope") or {}
        if report.get("operation_type", "patch") != "patch" or execution.get("status") not in _FINISHED:
            continue
        if environment and (scope.get("environment") != environment or scope.get("targeting") == "instance-id"):
            continue
        started = _ts(report.get("timestamp"))
        if not started or started < now - timedelta(days=LOOKBACK_DAYS):
            continue
        duration = execution.get("duration_seconds")
        ended = started + timedelta(seconds=float(duration) if duration is not None else MAX_RUN_SECONDS)
        gid = execution.get("plan_id") if scope.get("targeting") == "instance-id" and execution.get("plan_id") \
            else execution.get("execution_id") or report.get("report_id") or key
        g = groups.setdefault(gid, {"execution_id": execution.get("execution_id") or report.get("report_id"),
                                    "status": execution.get("status"), "started": started, "ended": ended,
                                    "known_duration": duration is not None, "instance_ids": [],
                                    "account_ids": [], "regions": []})
        g["started"], g["ended"] = min(g["started"], started), max(g["ended"], ended)
        g["known_duration"] = g["known_duration"] and duration is not None
        for field in ("instance_ids", "account_ids", "regions"):
            g[field] += [v for v in scope.get(field) or [] if v not in g[field]]
    covering = [g for g in groups.values() if not wanted or wanted <= set(g["instance_ids"])]
    if not covering:
        return None
    best = max(covering, key=lambda g: g["started"])
    return {
        "execution_id": best["execution_id"],
        "started_at": best["started"].isoformat(),
        "status": best["status"],
        "duration_seconds": (int((best["ended"] - best["started"]).total_seconds())
                             if best["known_duration"] else None),
        "since": int(best["started"].timestamp()) - BEFORE_SECONDS,
        "until": int(best["ended"].timestamp()) + (AFTER_SECONDS if best["known_duration"] else 0),
        "account_ids": best["account_ids"],
        "regions": best["regions"],
        "instance_ids": best["instance_ids"],
    }


def outcome_text(run: dict) -> str:
    """How the run a rollback undoes ended, for the plan: ", ended Failed after 28 s" or ""."""
    ended, took = run.get("status"), run.get("duration_seconds")
    if took is None:
        return f", ended {ended}" if ended else ""
    length = f"{took} s" if took < 120 else f"{round(took / 60)} min"
    if ended == "Cancelled":
        # The run ends at the stop; commands it had sent can go on for minutes.
        return f", stopped after {length}; commands it had sent may have run longer"
    return (f", ended {ended}" if ended else ", ran") + f" after {length}"
