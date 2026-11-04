"""Stored change plans and their one-time, approval-gated claim.

Write tools never execute. They store a plan here and return it. The UI's
approval API writes one decision per plan, plan-decisions/<plan_id>.json,
approved or cancelled, with If-None-Match so the first decision wins (the
agent's role is denied that prefix). The runtime claims the plan only when the
stored plan, its hash and a fresh approval for that hash all line up. A claim
writes a marker with If-None-Match so an approval can't be replayed, and
a plan past its expiry can't be claimed however recently it was approved.
"""

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

PLAN_TTL_MIN = 15
APPROVAL_TTL_MIN = 10
_PLAN_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class PlanError(Exception):
    """A plan can't be claimed; the message says why."""


def _s3():
    from helper.tools._shared import _s3 as shared_s3
    return shared_s3()


def _bucket() -> str:
    from helper.tools._shared import _get_compliance_bucket_name
    return _get_compliance_bucket_name()


def plan_hash(tool: str, params: dict, session_id: str, created_at: str) -> str:
    """SHA-256 over the canonical JSON of what the plan will run."""
    canonical = json.dumps({"tool": tool, "params": params, "session_id": session_id,
                            "created_at": created_at}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def create_plan(tool: str, params: dict, summary: dict, session_id: str, requested_by: str,
                view: Optional[dict] = None) -> dict:
    """Store a new plan.

    Args:
        tool: Name of the write tool whose execution the plan represents.
        params: Exact keyword arguments the executor will run with.
        summary: Human-facing plan details shown on the card.
        session_id: Conversation the plan belongs to.
        requested_by: Identity of the operator who asked for it.
        view: The change record the UI shows (helper.plan_view); not part of the hash.

    Returns:
        The stored plan record.
    """
    now = datetime.now(timezone.utc)
    created_at = now.isoformat()
    record = {
        "plan_id": uuid.uuid4().hex,
        "tool": tool,
        "params": params,
        "summary": summary,
        "session_id": session_id,
        "requested_by": requested_by,
        "created_at": created_at,
        "expires_at": (now + timedelta(minutes=PLAN_TTL_MIN)).isoformat(),
        "status": "awaiting_approval",
        "view": view,
    }
    record["plan_hash"] = plan_hash(tool, params, session_id, created_at)
    _s3().put_object(Bucket=_bucket(), Key=f"plans/{record['plan_id']}.json",
                     Body=json.dumps(record, default=str), ContentType="application/json",
                     IfNoneMatch="*")
    logger.info(f"[PLAN] created {record['plan_id']} tool={tool} session={session_id}")
    return record


def _get(key: str) -> Optional[dict]:
    try:
        body = _s3().get_object(Bucket=_bucket(), Key=key)["Body"].read()
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise
    return json.loads(body)


def get_record(key: str) -> Optional[dict]:
    """A JSON record from the plan bucket, or None if it doesn't exist."""
    return _get(key)


def put_record(key: str, record: dict, create_only: bool = False) -> bool:
    """Write a JSON record; with create_only, False when it already exists (If-None-Match)."""
    extra = {"IfNoneMatch": "*"} if create_only else {}
    try:
        _s3().put_object(Bucket=_bucket(), Key=key, Body=json.dumps(record, default=str),
                         ContentType="application/json", **extra)
    except ClientError as e:
        if create_only and e.response.get("Error", {}).get("Code") in ("PreconditionFailed", "ConditionalRequestConflict"):
            return False
        raise
    return True


def load_plan(plan_id: str) -> dict:
    """Load a plan and check its hash still matches its contents."""
    if not isinstance(plan_id, str) or not _PLAN_ID_RE.match(plan_id):
        raise PlanError("invalid plan id")
    plan = _get(f"plans/{plan_id}.json")
    if plan is None:
        raise PlanError("plan not found")
    try:
        expected = plan_hash(plan["tool"], plan["params"], plan["session_id"], plan["created_at"])
    except (KeyError, TypeError):
        raise PlanError("plan or approval record is malformed") from None
    if plan.get("plan_hash") != expected:
        raise PlanError("plan hash does not match its contents")
    return plan


def _decision(plan_id: str) -> Tuple[Optional[dict], bool]:
    """The plan's approval record, if any, and whether it was cancelled."""
    decision = _get(f"plan-decisions/{plan_id}.json") or {}
    if decision.get("decision") == "approved":
        return decision, False
    return None, decision.get("decision") == "cancelled"


def claim_plan(plan_id: str, session_id: str) -> Tuple[dict, dict]:
    """Claim an approved plan for execution, exactly once.

    Args:
        plan_id: Plan to run.
        session_id: Conversation asking to run it.

    Returns:
        (plan, approval) when every check passes.

    Raises:
        PlanError: when the plan can't be run, with the reason.
    """
    plan = load_plan(plan_id)
    if plan["session_id"] != session_id:
        raise PlanError("plan not found")  # same answer as a missing plan
    approval, cancelled = _decision(plan_id)
    if cancelled:
        raise PlanError("plan was cancelled")
    if approval is None:
        raise PlanError("plan is not approved")
    if approval.get("plan_hash") != plan["plan_hash"]:
        raise PlanError("approval is for a different plan hash")
    if approval.get("session_id") != plan["session_id"]:
        raise PlanError("approval is for another session")
    if approval.get("approver_role") != "operator":
        raise PlanError("approval was not made by an operator")
    try:
        approved_at = datetime.fromisoformat(approval["approved_at"])
        expires_at = datetime.fromisoformat(plan["expires_at"])
    except (KeyError, TypeError, ValueError):
        raise PlanError("plan or approval record is malformed") from None
    now = datetime.now(timezone.utc)
    if approved_at > expires_at:
        raise PlanError("plan expired before it was approved")
    if now > expires_at:
        raise PlanError("plan expired")
    if now - approved_at > timedelta(minutes=APPROVAL_TTL_MIN):
        raise PlanError("approval is stale")
    try:
        _s3().put_object(Bucket=_bucket(), Key=f"plan-runs/{plan_id}.json",
                         Body=json.dumps({"plan_id": plan_id, "claimed_at": now.isoformat(),
                                          "approver": approval.get("approver")}),
                         ContentType="application/json", IfNoneMatch="*")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("PreconditionFailed", "ConditionalRequestConflict"):
            raise PlanError("plan has already run")
        raise
    logger.info(f"[PLAN] claimed {plan_id} approver={approval.get('approver')}")
    return plan, approval
