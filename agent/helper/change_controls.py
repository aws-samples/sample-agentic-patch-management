"""Change controls around a run: Change Calendar, alarm abort, staged promotion.

- CHANGE_CALENDAR names an SSM Change Calendar. While it's closed no plan is
  made, and an approved plan doesn't start. If it can't be read, it counts as
  closed. It isn't watched during a run: a run that started while it was open
  carries on after it closes.
- ABORT_ALARMS lists CloudWatch alarms. Installs and rollbacks start with an
  alarm configuration on every target location, so SSM stops the run if one goes
  into ALARM in that location's account and Region.
- Promotion: a healthy verify_and_proceed records that a scope (a CVE, a
  severity, or everything) was verified in an environment. A staging plan
  expects a dev record whose run ended in the last PROMOTION_MAX_AGE_DAYS,
  prod a staging one. A missing stage warns on
  the plan card, or refuses the plan with REQUIRE_STAGED_ROLLOUT=true.
"""

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

STAGES = ("dev", "staging", "prod")
PROMOTION_MAX_AGE_DAYS = 14
_ALARM_NAME = re.compile(r"^[A-Za-z0-9_.:/#+=@-]{1,255}$")


def _ssm():
    from helper.tools._shared import _ssm as shared_ssm
    return shared_ssm()


def _s3():
    # Same storage as plans and approvals.
    from helper import plans
    return plans._s3()


def _bucket() -> str:
    from helper import plans
    return plans._bucket()


# ── Change Calendar ──────────────────────────────────────────────────

def calendar_closed() -> Optional[str]:
    """Why changes are blocked by the Change Calendar, or None when they aren't."""
    name = os.environ.get("CHANGE_CALENDAR", "").strip()
    if not name:
        return None
    try:
        state = _ssm().get_calendar_state(CalendarNames=[name])
    except Exception as e:
        logger.warning(f"[CHANGE_CALENDAR] {name} unreadable: {e}")
        return f"Change calendar {name} could not be read, so no change is made."
    if state.get("State") == "OPEN":
        return None
    reopens = state.get("NextTransitionTime")
    return f"Change calendar {name} is closed" + (f" until {reopens}." if reopens else ".")


# ── Alarm abort ──────────────────────────────────────────────────────

def abort_alarms() -> List[str]:
    names = [n.strip() for n in os.environ.get("ABORT_ALARMS", "").split(",")]
    return [n for n in names if n and _ALARM_NAME.match(n)]


def abort_alarms_problem() -> Optional[str]:
    """Why ABORT_ALARMS can't be used, or None. SSM's AlarmConfiguration takes exactly one alarm."""
    alarms = abort_alarms()
    if len(alarms) > 1:
        return (f"ABORT_ALARMS names {len(alarms)} alarms ({', '.join(alarms)}), but SSM watches one alarm per run. "
                "Combine them in a CloudWatch composite alarm and name that one.")
    return None


def alarm_configuration() -> Optional[dict]:
    """AlarmConfiguration for StartAutomationExecution, or None without abort alarms."""
    alarms = abort_alarms()
    if not alarms:
        return None
    return {"Alarms": [{"Name": a} for a in alarms], "IgnorePollAlarmFailure": False}


# ── Staged promotion ─────────────────────────────────────────────────

def scope_key(params: dict) -> str:
    """What a run patched, so promotion compares like with like."""
    if params.get("cve_id"):
        return f"cve:{str(params['cve_id']).upper()}"
    if params.get("severity_filter"):
        return f"severity:{str(params['severity_filter']).upper()}"
    return "all"


def _key(scope: str, environment: str) -> str:
    return f"promotions/{hashlib.sha256(scope.encode()).hexdigest()[:32]}/{environment}.json"


def scope_words(scope: str) -> str:
    """A scope key as the change record says it: CVE-2026-1234, HIGH and higher, all approved patches."""
    kind, _, value = scope.partition(":")
    if kind == "cve":
        return value
    if kind == "severity":
        return f"{value} and higher"
    return "all approved patches"


def record_promotion(environment: str, scope: str, execution_id: str, now: Optional[datetime] = None,
                     ended_at: Optional[str] = None) -> None:
    """Record a verified run. It ages from when the run ended, not from when it was verified
    (2 Nov review, T8): verifying a 20-day-old dev run today doesn't make it recent."""
    now = now or datetime.now(timezone.utc)
    rec = {"scope": scope, "environment": environment, "execution_id": execution_id,
           "verified_at": now.isoformat()}
    if ended_at:
        rec["run_ended_at"] = ended_at
    _s3().put_object(Bucket=_bucket(), Key=_key(scope, environment), ContentType="application/json",
                     Body=json.dumps(rec))
    logger.info(f"[PROMOTION] {scope} verified in {environment} ({execution_id}, ended {ended_at})")


def stage_check(environment: str, scope: str, now: Optional[datetime] = None) -> Tuple[Optional[str], Optional[datetime]]:
    """(missing, ended): the earlier stage with no recent verified run for this scope, or None,
    and when that stage's verified run ended. Records from before run_ended_at age from verified_at."""
    env = (environment or "").lower()
    stage = STAGES.index(env) if env in STAGES else len(STAGES) - 1  # unknown counts as prod
    if stage == 0:
        return None, None
    previous = STAGES[stage - 1]
    now = now or datetime.now(timezone.utc)
    try:
        rec = json.loads(_s3().get_object(Bucket=_bucket(), Key=_key(scope, previous))["Body"].read())
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return previous, None
        raise
    ended = datetime.fromisoformat(rec.get("run_ended_at") or rec["verified_at"])
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=timezone.utc)
    return (previous if now - ended > timedelta(days=PROMOTION_MAX_AGE_DAYS) else None), ended


def promotion_text(previous: str, scope: str, missing: bool, ended: Optional[datetime] = None) -> str:
    """What the promotion check found, exactly: which run, which scope, when it ended."""
    same = f"{previous} run with the same patch scope ({scope_words(scope)})"
    on = ended.astimezone(timezone.utc).strftime("%d %b %Y").lstrip("0") if ended else None
    if missing:
        return (f"No {same} finished healthy in the last {PROMOTION_MAX_AGE_DAYS} days"
                + (f"; the last one finished on {on}." if on else "."))
    return f"A {same} finished healthy " + (f"on {on}." if on else f"in the last {PROMOTION_MAX_AGE_DAYS} days.")


def missing_stage(environment: str, scope: str, now: Optional[datetime] = None) -> Optional[str]:
    """The earlier stage that hasn't been verified recently for this scope, or None."""
    return stage_check(environment, scope, now)[0]


def staged_rollout_required() -> bool:
    return os.environ.get("REQUIRE_STAGED_ROLLOUT", "").lower() == "true"
