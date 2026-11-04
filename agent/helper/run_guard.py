"""Patchy runs in progress, and whether a new change would patch the same instances.

Two runs on the same instances fight over the SSM instance lock and the yum
transaction history, and a rollback can't tell their changes apart. Each run
Patchy starts leaves a pending report (pending-reports/<execution id>.json)
with its scope until its report is written; SSM says whether it is still going.
A pending report SSM can't find is treated as finished.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from helper.concurrency import ContextThreadPoolExecutor as ThreadPoolExecutor

logger = logging.getLogger(__name__)

# Statuses of an execution that hasn't ended.
ACTIVE = {"Pending", "InProgress", "Waiting", "Cancelling", "Scheduled", "RunbookInProgress",
          "PendingApproval", "Approved", "PendingChangeCalendarOverride", "ChangeCalendarOverrideApproved"}
# Pending reports older than this are runs long finished whose report is stuck, not live runs.
LOOKBACK_HOURS = 24
_READ_WORKERS = 10


def _s3():
    from helper.tools._shared import _s3 as shared_s3
    return shared_s3()


def _bucket() -> str:
    from helper.tools._shared import _get_compliance_bucket_name
    return _get_compliance_bucket_name()


# A stopped or failed run ends in SSM before the commands it already sent; those instances
# are still patching until the commands finish (30 Oct: Stop at 14:43, commands done 14:46).
# The report waits for them (ui/api/reconcile.py COMMANDS_FINISH_HOURS); so does the guard.
FINISHING_HOURS = 3
_ENDED_EARLY = {"Cancelled", "Failed", "TimedOut", "CompletedWithFailure"}


def _status(execution_id: str) -> Tuple[Optional[str], Optional[datetime]]:
    """(SSM status, end time). None when SSM has no such execution; "Unknown" when it couldn't be read."""
    from helper.tools._shared import _ssm
    try:
        ex = _ssm().get_automation_execution(AutomationExecutionId=execution_id)["AutomationExecution"]
    except Exception as e:
        if "AutomationExecutionNotFound" in f"{getattr(e, 'response', {}).get('Error', {}).get('Code', '')} {e}":
            return None, None
        # Throttled or denied: not proof the run has finished (2 Nov review, M7).
        logger.warning(f"[RUN_GUARD] status of {execution_id} couldn't be read: {e}")
        return "Unknown", None
    end = ex.get("ExecutionEndTime")
    if isinstance(end, datetime) and end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return ex.get("AutomationExecutionStatus"), end


def _pending_contexts(now: datetime) -> List[Tuple[str, Optional[dict]]]:
    """(key, context) for each recent pending report; context None when it couldn't be read."""
    s3, bucket = _s3(), _bucket()
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="pending-reports/"):
        for obj in page.get("Contents") or []:
            modified = obj["LastModified"]
            modified = modified if modified.tzinfo else modified.replace(tzinfo=timezone.utc)
            if obj["Key"].endswith(".json") and modified >= now - timedelta(hours=LOOKBACK_HOURS):
                keys.append(obj["Key"])

    def read(key: str) -> Tuple[str, Optional[dict]]:
        try:
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        except Exception as e:
            logger.warning(f"[RUN_GUARD] pending report {key} couldn't be read: {e}")
            return key, None
        try:
            ctx = json.loads(body)
        except ValueError:
            return key, {}  # malformed: the reconciler drops these; it describes no run
        return key, ctx if isinstance(ctx, dict) else {}

    if not keys:
        return []
    with ThreadPoolExecutor(max_workers=min(_READ_WORKERS, len(keys))) as pool:
        return list(pool.map(read, keys))


def active_runs(now: Optional[datetime] = None) -> List[dict]:
    """Runs that may still be patching, with their scope: in progress in SSM, stopped or failed
    recently with their report still pending (commands may still be running), or whose state
    couldn't be confirmed. A pending report that couldn't be read counts, with an unknown scope."""
    now = now or datetime.now(timezone.utc)

    def check(item: Tuple[str, Optional[dict]]) -> Optional[dict]:
        key, ctx = item
        if ctx is None:
            return {**_scope({"execution_id": key.rsplit("/", 1)[-1][:-5]}), "status": "Unknown"}
        if not ctx.get("execution_id"):
            return None
        status, ended = _status(ctx["execution_id"])
        if status in ACTIVE or status == "Unknown":
            return {**_scope(ctx), "status": status}
        if status in _ENDED_EARLY and (ended is None or now - ended < timedelta(hours=FINISHING_HOURS)):
            return {**_scope(ctx), "status": "finishing"}
        return None

    items = _pending_contexts(now)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(_READ_WORKERS, len(items))) as pool:
        return [r for r in pool.map(check, items) if r]


def unreported_runs(scope: dict, now: Optional[datetime] = None) -> List[dict]:
    """Patch runs overlapping this scope whose report isn't written yet, whatever their state.

    A rollback undoes the last reported run; while a newer one is unreported, the last
    reported run is the wrong one to undo (2 Nov review, T1).
    """
    now = now or datetime.now(timezone.utc)
    out = []
    fleet = None
    for key, ctx in _pending_contexts(now):
        if ctx is None:
            out.append({**_scope({"execution_id": key.rsplit("/", 1)[-1][:-5]}), "status": "unreadable"})
            continue
        if not ctx.get("execution_id") or ctx.get("operation_type", "patch") != "patch":
            continue
        run = _scope(ctx)
        fleet = _fleet() if fleet is None else fleet
        if overlaps(run, scope, fleet):
            out.append({**run, "status": "unreported"})
    return out


def _scope(ctx: dict) -> dict:
    return {"execution_id": ctx.get("execution_id"), "plan_id": ctx.get("plan_id"),
            "operation_type": ctx.get("operation_type", "patch"), "operator": ctx.get("operator"),
            "targeting": ctx.get("targeting"), "environment": ctx.get("environment"),
            "instance_ids": list(ctx.get("instance_ids") or []),
            "account_ids": list(ctx.get("account_ids") or []), "regions": list(ctx.get("regions") or [])}


def _meets(a: List[str], b: List[str]) -> bool:
    """Lists overlap; an empty list means unknown, which counts as overlapping."""
    return not a or not b or bool(set(a) & set(b))


def _fleet() -> Dict[str, dict]:
    from helper.tools import _shared
    try:
        _shared._get_fleet_summary()
        return _shared.fleet_instances() or {}
    except Exception:
        return {}


def _env_holds(env: Optional[str], accounts: List[str], ids: List[str], fleet: Dict[str, dict]) -> bool:
    """Whether any of these instances may be in this environment (and accounts); unknown ones may."""
    from helper.tools._shared import _normalize_environment
    want = _normalize_environment(env or "")
    for iid in ids:
        inst = fleet.get(iid)
        if not inst:
            return True
        if _normalize_environment(inst.get("environment") or "") == want and \
                (not accounts or not inst.get("account_id") or inst["account_id"] in accounts):
            return True
    return False


def overlaps(run: dict, scope: dict, fleet: Optional[Dict[str, dict]] = None) -> bool:
    """Whether a run in progress may patch any instance this scope targets.

    scope: {instance_ids} for an instance-ID change, or {environment, account_ids, regions}.
    """
    from helper.tools._shared import _normalize_environment
    if not run.get("environment") and not run.get("instance_ids"):
        return True  # a run whose scope couldn't be read may cover anything
    if not _meets(run.get("regions") or [], scope.get("regions") or []):
        return False
    run_ids, want_ids = run.get("instance_ids") or [], scope.get("instance_ids") or []
    if run_ids and want_ids:
        return bool(set(run_ids) & set(want_ids))
    if not _meets(run.get("account_ids") or [], scope.get("account_ids") or []):
        return False
    fleet = _fleet() if fleet is None else fleet
    if run_ids:
        return _env_holds(scope.get("environment"), scope.get("account_ids") or [], run_ids, fleet)
    if want_ids:
        return _env_holds(run.get("environment"), run.get("account_ids") or [], want_ids, fleet)
    return _normalize_environment(run.get("environment") or "") == _normalize_environment(scope.get("environment") or "")


def blocking_run(scope: dict) -> Optional[dict]:
    """The first run in progress that overlaps this scope, or None. A failed check doesn't block."""
    try:
        runs = active_runs()
    except Exception as e:
        logger.warning(f"[RUN_GUARD] couldn't list runs in progress: {e}")
        return None
    fleet = _fleet() if runs else {}
    return next((r for r in runs if overlaps(r, scope, fleet)), None)


# One plan starts at a time. The lock is a single record: created once with If-None-Match,
# then taken over with If-Match on its ETag once released or expired, so two starts can't
# both hold it. The runtime can't delete, so a start releases it by marking it released.
START_LOCK_KEY = "run-locks/start.json"
START_LOCK_SECONDS = 300
_CONFLICT = ("PreconditionFailed", "ConditionalRequestConflict")


def _code(e: Exception) -> str:
    return (getattr(e, "response", None) or {}).get("Error", {}).get("Code", "")


def _lock_store():
    # Same storage as plans and approvals.
    from helper import plans
    return plans._s3(), plans._bucket()


def take_start_lock(plan_id: str, now: Optional[datetime] = None) -> Tuple[Optional[str], Optional[str]]:
    """(ETag of the lock now held, None), or (None, why not) while another plan is starting.

    Raises when the lock can't be read or written; the caller refuses then.
    """
    s3, bucket = _lock_store()
    now = now or datetime.now(timezone.utc)
    body = json.dumps({"plan_id": plan_id, "taken_at": now.isoformat(), "released": False}).encode()
    put = dict(Bucket=bucket, Key=START_LOCK_KEY, Body=body, ContentType="application/json")
    try:
        return s3.put_object(**put, IfNoneMatch="*")["ETag"], None
    except Exception as e:
        if _code(e) not in _CONFLICT:
            raise
    held = s3.get_object(Bucket=bucket, Key=START_LOCK_KEY)
    try:
        rec = json.loads(held["Body"].read())
        taken = datetime.fromisoformat(rec["taken_at"])
    except (ValueError, KeyError, TypeError):
        rec, taken = {}, None
    age = (now - taken).total_seconds() if taken else None
    if not rec.get("released") and age is not None and age < START_LOCK_SECONDS and rec.get("plan_id") != plan_id:
        return None, (f"Another Patchy plan ({str(rec.get('plan_id'))[:6]}) began starting {max(0, int(age))}s ago "
                      "and hasn't recorded its runs yet, so Patchy can't check whether it covers the same instances. "
                      "Start this one again in a minute.")
    try:
        return s3.put_object(**put, IfMatch=held["ETag"])["ETag"], None
    except Exception as e:
        if _code(e) not in _CONFLICT:
            raise
        return None, ("Another Patchy plan began starting at the same moment, so Patchy can't check whether it "
                      "covers the same instances. Start this one again in a minute.")


def release_start_lock(etag: str, plan_id: str) -> None:
    """Mark the lock released once the runs and their pending reports are written. If this fails,
    the lock expires after START_LOCK_SECONDS."""
    try:
        s3, bucket = _lock_store()
        s3.put_object(Bucket=bucket, Key=START_LOCK_KEY, ContentType="application/json", IfMatch=etag,
                         Body=json.dumps({"plan_id": plan_id, "released": True,
                                          "taken_at": datetime.now(timezone.utc).isoformat()}).encode())
    except Exception as e:
        logger.warning(f"[RUN_GUARD] start lock for {plan_id} not released ({e}); it expires in "
                       f"{START_LOCK_SECONDS}s")


def describe(run: dict) -> str:
    what = "rollback" if run.get("operation_type") == "rollback" else "patch run"
    where = (f"{len(run['instance_ids'])} instance(s)" if run.get("instance_ids")
             else f"{run.get('environment') or 'an environment'}")
    by = f" started by {run['operator']}" if run.get("operator") else ""
    state = {"finishing": "finishing: it was stopped or failed, and the patch commands it already sent may still be running",
             "Unknown": "of unknown state: Patchy couldn't read it, so it can't confirm the run has finished"
             }.get(run.get("status"), f"still {run.get('status')}")
    return f"A Patchy {what} on {where}{by} is {state} (run {str(run.get('execution_id'))[:8]})"
