"""Compliance report reconciliation, shared by the UI API and the report Lambda.

When a run starts, the agent writes pending-reports/<execution_id>.json with
the run's context. Once the Automation execution finishes, reconciliation
counts outcomes across every account and region, works out the SLA result
and writes the final report. The dashboard calls this on load; the report
Lambda calls it when EventBridge says a Patchy run finished, so reports no
longer wait for someone to open the dashboard.

Only boto3 and the standard library, so the Lambda bundles this file alone.
"""

import json
import re
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger("reconcile")

# The dashboard and the report Lambda only read from spokes: the read-only role.
SPOKE_READ_ROLE = os.environ.get("SPOKE_READ_ROLE", "PatchySpokeReadRole")
_DEFAULT_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"

_hub_account_id_cache: "str | None" = None
_sts_credential_cache: "dict[str, dict]" = {}
_sts_cache_lock = threading.Lock()



def _report_tag_scope(context: dict) -> dict:
    """The report's tags (REPORT_TAGS, recorded when the run started) and the values of mixed ones."""
    return {"tags": dict(context.get("report_tags") or {}), "tag_values": dict(context.get("report_tag_values") or {})}


def _tag_metadata(context: dict) -> dict:
    """S3 metadata for the report tags: tag-<name>, lower case, other characters as hyphens."""
    out = {}
    for key, value in (context.get("report_tags") or {}).items():
        name = re.sub(r"[^a-z0-9]+", "-", str(key).lower()).strip("-")
        if name:
            out[f"tag-{name}"] = str(value).encode("ascii", "replace").decode()[:256]
    return out

class _CachedSession:
    """A boto3 Session that makes each client once.

    A fresh boto3.Session loads its own copy of every service model it makes a
    client for, so building sessions per request ran the UI task out of memory.
    Spoke sessions share the hub's boto3 Session (and its model cache) and pass
    their credentials to each client. Clients are thread-safe once made; making
    them from a shared session isn't, so creation takes one process-wide lock.
    """

    def __init__(self, session, credentials=None):
        self._s = session
        self._credentials = credentials or {}
        self._clients: dict = {}

    def client(self, service, region_name=None, **kwargs):
        region = region_name or self._s.region_name
        config = kwargs.get("config")
        key = (service, region, repr(getattr(config, "_user_provided_options", config)),
               tuple(sorted((k, repr(v)) for k, v in kwargs.items() if k != "config")))
        c = self._clients.get(key)
        if c is None:
            with _client_create_lock:
                c = self._clients.get(key)
                if c is None:
                    c = self._clients[key] = self._s.client(service, region_name=region,
                                                            **self._credentials, **kwargs)
        return c

    def __getattr__(self, name):
        if self._credentials:  # a spoke session must not hand out the hub's session
            raise AttributeError(name)
        return getattr(self._s, name)


_client_create_lock = threading.Lock()
_hub_session = None
_hub_session_lock = threading.Lock()


def _aws_session():
    """The hub session, made once per process."""
    global _hub_session
    with _hub_session_lock:
        if _hub_session is None:
            profile = os.environ.get("AWS_PROFILE")
            _hub_session = _CachedSession(boto3.Session(profile_name=profile, region_name=_DEFAULT_REGION) if profile
                                          else boto3.Session(region_name=_DEFAULT_REGION))
        return _hub_session


def _sts_client():
    return _aws_session().client("sts")


def _get_hub_account_id() -> str:
    global _hub_account_id_cache
    if not _hub_account_id_cache:
        _hub_account_id_cache = _aws_session().client("sts").get_caller_identity()["Account"]
    return _hub_account_id_cache


_spoke_locks: "dict[str, threading.Lock]" = {}


def _get_spoke_session(account_id: str):
    """Clients on the read-only spoke role, reused until the credentials are 5 minutes from expiry.

    One assume-role per account at a time; the clients come from the hub's session.
    """
    with _sts_cache_lock:
        lock = _spoke_locks.setdefault(account_id, threading.Lock())
    with lock:
        cached = _sts_credential_cache.get(account_id)
        if cached and cached["expiry"] - time.time() > 300:
            return cached["session"]
        role_arn = f"arn:aws:iam::{account_id}:role/{SPOKE_READ_ROLE}"
        resp = _sts_client().assume_role(RoleArn=role_arn, RoleSessionName=f"patchy-dashboard-{account_id}",
                                         DurationSeconds=3600)
        creds = resp["Credentials"]
        session = _CachedSession(_aws_session()._s, {
            "aws_access_key_id": creds["AccessKeyId"],
            "aws_secret_access_key": creds["SecretAccessKey"],
            "aws_session_token": creds["SessionToken"],
        })
        _sts_credential_cache[account_id] = {"session": session, "expiry": creds["Expiration"].timestamp()}
        return session


def _read_report_json(s3, bucket: str, key: str) -> dict | None:
    """Read a single report JSON from S3. Returns parsed dict or None."""
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
        return json.loads(resp["Body"].read())
    except Exception:
        return None


# Command invocation statuses that haven't ended.
_COMMAND_IN_FLIGHT = {"Pending", "InProgress", "Delayed", "Cancelling"}
# An Automation iteration that hasn't ended; its command may not be sent or listed yet.
_ITERATION_ACTIVE = {"Pending", "InProgress", "Waiting", "Cancelling", "PendingApproval", "Scheduled", "RunbookInProgress", "Exiting"}
# How long after a run ends its report waits for commands still going; then it's written as it stands.
COMMANDS_FINISH_HOURS = 3


def _ended_within(exec_data: dict, hours: float) -> bool:
    ended = exec_data.get('ExecutionEndTime')
    if not ended:
        return False
    ended = ended if isinstance(ended, datetime) else datetime.fromisoformat(str(ended))
    ended = ended if ended.tzinfo else ended.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - ended < timedelta(hours=hours)


def _finishing_entry(execution_id: str, context: dict, outcomes: dict) -> dict:
    """A run that has ended in SSM while the patch commands it sent are still going."""
    return {
        'execution_id': execution_id,
        'operation_type': context.get('operation_type', 'patch'),
        'environment': context.get('environment', 'unknown'),
        'started_at': context.get('started_at'),
        'targeting': context.get('targeting'),
        'instance_count': outcomes.get('instance_count') or len(context.get('instance_ids', [])),
        'status': 'CommandsFinishing',
    }


def _count_execution_outcomes(ssm_hub, execution_id: str, hub_region: str) -> dict:
    """Walk a parent Automation's child SendCommand invocations to count outcomes.

    Returns {success_count, failure_count, instance_count, instance_ids,
    still_running, incomplete, unread_locations} aggregated across every
    (account, region) child of the parent execution.
    Used by the compliance reconciler to populate fields the agent can't
    supply at start time — for tag-based runs the instance list isn't known
    until SSM resolves the tag query.

    Cross-account: child Automation executions live in spoke accounts and
    their CommandIds are spoke-scoped. We assume the read role into each
    child's owning account before calling list_command_invocations. Without
    this, ListCommandInvocations from the hub silently returns zero results
    even though the patches actually ran.

    A lookup that fails is logged and skipped, and the counts are partial:
    incomplete is then True and unread_locations names where the outcomes
    couldn't be read ("<account> <region>", "hub <region>", or a child or the
    run itself when its location isn't known).
    """
    # One outcome per instance: an instance can appear in more than one command
    # (a retried step, or a location walked by two paths), and a failure anywhere wins.
    outcome: dict[str, str] = {}
    # Commands still going. A stopped run ends before the commands it already
    # sent, and those instances finish patching (30 Oct: Stop at 14:43:26, all
    # five commands Success by 14:46), so they're counted once they end.
    in_flight: set[str] = set()
    # Where outcomes couldn't be read, so the counts leave those instances out.
    unread: set[str] = set()

    def _where(account_id: str, region: str) -> str:
        return f"{account_id or 'hub'} {region or hub_region}"

    # Cache one ssm client per (account, region) so we don't repeat assume-role
    # for siblings of the same child. Hub session keyed by ('', '').
    ssm_clients: dict[tuple[str, str], object] = {}

    def _ssm_client_for(account_id: str, region: str):
        # The hub is reachable with our own credentials, and assuming a spoke
        # role into our own account fails with AccessDenied — so normalise the
        # hub account to the hub session rather than paying a failed STS call
        # for every same-account child.
        if account_id:
            try:
                if account_id == _get_hub_account_id():
                    account_id = ""
            except Exception:
                pass  # Can't resolve our own account; fall through and try.
        # Empty account_id means use the hub session.
        key = (account_id or "", region or hub_region)
        if key in ssm_clients:
            return ssm_clients[key]
        if not account_id:
            client = _aws_session().client("ssm", region_name=region or hub_region)
        else:
            try:
                spoke_session = _get_spoke_session(account_id)
                client = spoke_session.client("ssm", region_name=region or hub_region)
            except Exception as exc:
                logger.warning(
                    f"[RECONCILE] Could not assume role into {account_id} for {execution_id}: {exc}"
                )
                # Fallback to hub session — unlikely to find anything but
                # avoids an exception breaking the loop. The location counts as unread.
                unread.add(_where(account_id, region))
                client = _aws_session().client("ssm", region_name=region or hub_region)
        ssm_clients[key] = client
        return client

    def _tally_command(client, command_id: str, where: str) -> None:
        next_token = None
        while True:
            kwargs = {"CommandId": command_id, "MaxResults": 50}
            if next_token:
                kwargs["NextToken"] = next_token
            try:
                resp = client.list_command_invocations(**kwargs)
            except Exception as exc:
                logger.warning(f"[RECONCILE] list_command_invocations failed for {command_id}: {exc}")
                unread.add(where)
                return
            for inv in resp.get("CommandInvocations", []):
                iid, status = inv["InstanceId"], inv.get("Status", "Unknown")
                if status in ("Cancelled", "TimedOut", "Failed"):
                    outcome[iid] = "failure"
                elif status == "Success":
                    outcome.setdefault(iid, "success")
                else:
                    outcome.setdefault(iid, "other")
                if status in _COMMAND_IN_FLIGHT:
                    in_flight.add(iid)
            next_token = resp.get("NextToken")
            if not next_token:
                break

    def _walk_steps(client, steps: list, where: str, depth: int = 0) -> None:
        """Walk automation steps to find and tally SendCommand invocations.

        Recurses into aws:executeAutomation steps (up to depth 3) to handle
        the 3-level tree created by instance-ID patching with TargetLocations:
          Parent → Child (per-account) → Grandchild (per-instance) → aws:runCommand
        """
        if depth > 3:
            return
        for step in steps or []:
            outputs = step.get("Outputs") or {}
            cmd_id = outputs.get("CommandId", [None])[0]
            if cmd_id:
                _tally_command(client, cmd_id, where)
            elif step.get("Action") == "aws:executeAutomation":
                # Recurse into sub-executions (handles instance-ID TargetLocations)
                for exec_id in outputs.get("ExecutionId", []) or outputs.get("AutomationExecutionId", []) or []:
                    if not exec_id:
                        continue
                    try:
                        sub_exec = client.get_automation_execution(
                            AutomationExecutionId=exec_id
                        )["AutomationExecution"]
                        _walk_steps(client, sub_exec.get("StepExecutions", []), where, depth + 1)
                    except Exception as exc:
                        logger.warning(f"[RECONCILE] Could not walk sub-execution {exec_id}: {exc}")
                        unread.add(where)

    def _walk_iterations(client, parent_id: str, where: str) -> None:
        """Tally the executions SSM started under parent_id in this location, and their steps."""
        token = None
        while True:
            try:
                resp = client.describe_automation_executions(
                    Filters=[{"Key": "ParentExecutionId", "Values": [parent_id]}],
                    MaxResults=50, **({"NextToken": token} if token else {}))
            except Exception as exc:
                logger.warning(f"[RECONCILE] Could not list iterations of {parent_id}: {exc}")
                unread.add(where)
                return
            for meta in resp.get("AutomationExecutionMetadataList", []):
                iteration = meta.get("AutomationExecutionId")
                if not iteration:
                    continue
                if meta.get("AutomationExecutionStatus") in _ITERATION_ACTIVE:
                    # Not finished, so its command may not be listed yet: count the run as going.
                    in_flight.add(f"iteration {iteration}")
                try:
                    sub = client.get_automation_execution(AutomationExecutionId=iteration)["AutomationExecution"]
                    _walk_steps(client, sub.get("StepExecutions", []), where, 1)
                except Exception as exc:
                    logger.warning(f"[RECONCILE] Could not walk iteration {iteration}: {exc}")
                    unread.add(where)
            token = resp.get("NextToken")
            if not token:
                return

    # Parent execution lives in the hub.
    try:
        parent = ssm_hub.get_automation_execution(
            AutomationExecutionId=execution_id
        )["AutomationExecution"]
    except Exception as exc:
        logger.warning(f"[RECONCILE] get_automation_execution failed for {execution_id}: {exc}")
        return {"success_count": 0, "failure_count": 0, "instance_count": 0, "instance_ids": [],
                "still_running": 0, "incomplete": True, "unread_locations": [f"run {execution_id}"]}

    # One step per location; past a size limit SSM truncates the list on the execution.
    parent_steps = list(parent.get("StepExecutions") or [])
    if parent.get("StepExecutionsTruncated"):
        try:
            parent_steps = []
            for page in ssm_hub.get_paginator("describe_automation_step_executions").paginate(
                    AutomationExecutionId=execution_id):
                parent_steps.extend(page.get("StepExecutions") or [])
        except Exception as exc:
            logger.warning(f"[RECONCILE] describe_automation_step_executions failed for {execution_id}: {exc}")
            parent_steps = list(parent.get("StepExecutions") or [])
            unread.add(f"run {execution_id} (step list)")

    def _located(step) -> bool:
        """A per-location step ("<account>_<region>"): its child is read below, from its own account."""
        account, _, region = str(step.get("StepName", "")).partition("_")
        return account.isdigit() and bool(region)

    # Path A (instance-ID, no TargetLocations) emits the CommandId directly
    # on the parent's StepExecutions and runs in the hub.
    _walk_steps(_ssm_client_for("", hub_region), [st for st in parent_steps if not _located(st)], _where("", hub_region))

    # Path A with TargetLocations (now the default for instance-ID runs) and
    # Path B (tag-based) both spawn child executions. Two ways to find them:
    #   1. describe_automation_executions(ParentExecutionId=...) — works for
    #      cross-account, sometimes empty for same-account dispatch
    #   2. parent's `aws:executeAutomation` step Outputs — always reliable
    # Use both and dedupe so we never miss a child.
    child_id_set: set[str] = set()

    # Source 1: describe_automation_executions
    try:
        children_meta, token = [], None
        while True:  # one child per location, so a large run has more than one page
            resp = ssm_hub.describe_automation_executions(
                Filters=[{"Key": "ParentExecutionId", "Values": [execution_id]}],
                MaxResults=50, **({"NextToken": token} if token else {}))
            children_meta += resp.get("AutomationExecutionMetadataList", [])
            token = resp.get("NextToken")
            if not token:
                break
    except Exception as exc:
        logger.warning(f"[RECONCILE] describe_automation_executions failed for {execution_id}: {exc}")
        children_meta = []

    children: list[dict] = []
    for child in children_meta:
        cid = child.get("AutomationExecutionId")
        if cid and cid not in child_id_set:
            child_id_set.add(cid)
            children.append(child)

    # Source 2: parent's aws:executeAutomation step Outputs. The dispatcher
    # step's Outputs include the spawned child execution IDs even when
    # describe_automation_executions hasn't surfaced them yet.
    for step in parent_steps:
        if step.get("Action") != "aws:executeAutomation":
            continue
        # With TargetLocations each step is named "<account>_<region>": where the child lives.
        account, _, region = str(step.get("StepName", "")).partition("_")
        hint = (account, region) if account.isdigit() and region else None
        outputs = step.get("Outputs") or {}
        for key in ("ExecutionId", "AutomationExecutionId"):
            for cid in outputs.get(key, []) or []:
                if cid and cid not in child_id_set:
                    child_id_set.add(cid)
                    # Synthetic child entry — only the ID (and where it ran) is needed since
                    # the loop below fetches full details via get_automation_execution.
                    children.append({"AutomationExecutionId": cid, "_location": hint})
                elif cid and hint:
                    for c in children:
                        if c.get("AutomationExecutionId") == cid and not c.get("_location"):
                            c["_location"] = hint

    # (account, region) pairs the parent dispatched to. A cross-account child
    # execution lives in one of these accounts, and hub credentials cannot read
    # it, so we need the right spoke client to fetch it at all. Reading the
    # child with the hub client is the bug this list exists to avoid: it raises
    # AutomationExecutionNotFound for every spoke child, which previously meant
    # they were all skipped and the report recorded zero instances.
    parent_locations: list[tuple[str, str]] = []
    for loc in parent.get("TargetLocations") or []:
        for acct in loc.get("Accounts") or []:
            for reg in (loc.get("Regions") or [hub_region]):
                if (acct, reg) not in parent_locations:
                    parent_locations.append((acct, reg))

    def _account_from_executed_by(child_meta: dict) -> str:
        """Pull the owning account out of a child's ExecutedBy ARN.

        AutomationExecutionMetadata carries no account field. ExecutedBy is an
        assumed-role ARN — arn:aws:sts::<account>:assumed-role/... — and for a
        cross-account child that account is the spoke. Note `Target` is a plain
        string (the target resource id), not a structure, so it is no help here.
        """
        arn = child_meta.get("ExecutedBy") or ""
        parts = arn.split(":")
        if len(parts) >= 5 and parts[4].isdigit():
            return parts[4]
        return ""

    def _fetch_child(child_meta: dict, child_id: str):
        """Fetch a child execution, trying each account it could live in.

        Returns (execution, account, region) or (None, None, None). A child
        spawned by TargetLocations lives in the spoke account and is not
        readable with hub credentials at all, so the account has to be right.
        Cheapest correct guess first.
        """
        candidates: list[tuple[str, str]] = []
        regions = [r for _, r in parent_locations] or [hub_region]

        # 0. The location named by the parent step that started it.
        if child_meta.get("_location"):
            candidates.append(child_meta["_location"])

        # 1. The account named in the child's own ExecutedBy ARN.
        executed_by = _account_from_executed_by(child_meta)
        if executed_by:
            for reg in regions:
                if (executed_by, reg) not in candidates:
                    candidates.append((executed_by, reg))

        # 2. The hub, for same-account dispatch (AutomationType 'Local').
        if ("", hub_region) not in candidates:
            candidates.append(("", hub_region))

        # 3. Every account/region the parent dispatched to.
        for pair in parent_locations:
            if pair not in candidates:
                candidates.append(pair)

        last_exc = None
        for acct, reg in candidates:
            try:
                client = _ssm_client_for(acct, reg)
                execution = client.get_automation_execution(
                    AutomationExecutionId=child_id
                )["AutomationExecution"]
                return execution, acct, reg
            except Exception as exc:
                last_exc = exc
                continue

        logger.warning(
            f"[RECONCILE] child {child_id} not readable from any of "
            f"{[a or 'hub' for a, _ in candidates]}: {last_exc}"
        )
        return None, None, None

    for child in children:
        child_id = child.get("AutomationExecutionId")
        if not child_id:
            continue

        child_full, child_account, child_region = _fetch_child(child, child_id)
        if child_full is None:
            hint = child.get("_location")
            unread.add(_where(*hint) if hint else f"child {child_id}")
            continue

        # The child's own TargetLocations is the authoritative account/region,
        # when present — prefer it over whichever candidate happened to work.
        target_locs = child_full.get("TargetLocations") or []
        if target_locs:
            accts = target_locs[0].get("Accounts") or []
            regions = target_locs[0].get("Regions") or []
            if accts:
                child_account = accts[0]
            if regions:
                child_region = regions[0]

        try:
            client = _ssm_client_for(child_account, child_region or hub_region)
            where = _where(child_account, child_region)
            _walk_steps(client, child_full.get("StepExecutions", []), where)
            # An instance-ID run's location child runs one iteration per instance; its steps
            # carry no execution IDs, so the iterations are found by their parent instead
            # (disruption round, 2 Nov: stopped runs were reported before their commands ended).
            _walk_iterations(client, child_id, where)
        except Exception as exc:
            logger.warning(
                f"[RECONCILE] failed to walk child {child_id} "
                f"(account={child_account or 'hub'} region={child_region}): {exc}"
            )
            unread.add(_where(child_account, child_region))
            continue

    return {
        "success_count": sum(1 for o in outcome.values() if o == "success"),
        "failure_count": sum(1 for o in outcome.values() if o == "failure"),
        "instance_count": len(outcome),
        "instance_ids": sorted(outcome),
        "still_running": len(in_flight),
        "incomplete": bool(unread),
        "unread_locations": sorted(unread),
    }


def _patch_state_client(account_id: str, region: str):
    """SSM client in an instance's account and region (hub credentials for the hub)."""
    if not account_id or account_id == _get_hub_account_id():
        return _aws_session().client("ssm", region_name=region)
    return _get_spoke_session(account_id).client("ssm", region_name=region)


def _pending_reboots(instance_ids: list, account_ids: list, regions: list) -> list:
    """Instances that still have patches installed pending a reboot.

    The context records the run's accounts and regions but not each instance's
    location, so each (account, region) is asked about every instance; patch
    states only come back where the instance lives. Best effort.
    """
    pending = set()
    for account in account_ids or [None]:
        for region in regions or [_DEFAULT_REGION]:
            try:
                ssm = _patch_state_client(account, region)
                for i in range(0, len(instance_ids), 50):
                    resp = ssm.describe_instance_patch_states(InstanceIds=instance_ids[i:i + 50])
                    pending |= {s["InstanceId"] for s in resp.get("InstancePatchStates", [])
                                if s.get("InstalledPendingRebootCount", 0) > 0}
            except Exception as exc:
                logger.warning(f"[RECONCILE] patch states unavailable for {account}/{region}: {exc}")
    return sorted(pending)


# A pending context whose execution can't be read after this long is dropped,
# so it doesn't sit in the queue for ever.
PENDING_MAX_AGE_DAYS = 7


def _reconcile_pending_reports(s3, bucket: str, session, write: bool = True,
                               only: "set[str] | None" = None, deadline: "float | None" = None) -> tuple[int, list[dict]]:
    """Process pending compliance contexts and generate final reports.

    Walks s3://{bucket}/pending-reports/, checks each automation execution status,
    and for completed ones (Success/Failed/Cancelled/TimedOut) generates the final
    compliance report and deletes the pending file. With write=False it only
    lists the runs still going (the dashboard, when the report Lambda writes).

    Returns: (number of reports generated, list of running operations).

    Idempotent: a final report is written with If-None-Match, so two writers
    (the Lambda and a dashboard) can't both write it, and an existing report
    just clears the pending file.

    only: reconcile just these executions (a run-end event handles its own run).
    deadline: stop starting new reports after this time (epoch seconds); the
    next sweep picks up the rest.
    """
    generated_count = 0
    running: list[dict] = []
    try:
        pending_objects = []
        # One run: list just its key, which is cheap and empty when there's nothing pending.
        prefix = f"pending-reports/{next(iter(only))}.json" if only and len(only) == 1 else 'pending-reports/'
        for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=prefix):
            pending_objects.extend(page.get('Contents', []))
    except Exception as e:
        logger.warning(f"[RECONCILE] Could not list pending-reports: {e}")
        return 0, []

    if not pending_objects:
        return 0, []

    ssm = session.client('ssm')

    for obj in pending_objects:
        key = obj['Key']
        if not key.endswith('.json'):
            continue
        execution_id = key.replace('pending-reports/', '').replace('.json', '')
        if only is not None and execution_id not in only:
            continue
        if deadline is not None and time.time() > deadline:
            logger.warning("[RECONCILE] Out of time; the next sweep picks up the remaining pending reports")
            break

        context = None
        try:
            # Read pending context
            ctx_resp = s3.get_object(Bucket=bucket, Key=key)
            try:
                context = json.loads(ctx_resp['Body'].read())
            except ValueError:
                # It can never be processed; keeping it would retry it on every sweep.
                if write:
                    s3.delete_object(Bucket=bucket, Key=key)
                    logger.warning(f"[RECONCILE] Dropped pending {execution_id}: not valid JSON")
                continue

            # Check execution status
            try:
                exec_data = ssm.get_automation_execution(
                    AutomationExecutionId=execution_id
                )['AutomationExecution']
                status = exec_data.get('AutomationExecutionStatus', 'Unknown')
            except Exception as e:
                logger.warning(f"[RECONCILE] Could not get status for {execution_id}: {e}")
                written = _coerce_utc_dt(context.get('started_at')) or obj.get('LastModified')
                if write and written and datetime.now(timezone.utc) - written > timedelta(days=PENDING_MAX_AGE_DAYS):
                    s3.delete_object(Bucket=bucket, Key=key)
                    logger.warning(f"[RECONCILE] Dropped pending {execution_id}: unreadable for over "
                                   f"{PENDING_MAX_AGE_DAYS} days")
                continue

            # Skip if still in progress — collect as running operation
            if is_active(status):
                running.append({
                    'execution_id': execution_id,
                    'operation_type': context.get('operation_type', 'patch'),
                    'environment': context.get('environment', 'unknown'),
                    'started_at': context.get('started_at'),
                    'targeting': context.get('targeting'),
                    'instance_count': len(context.get('instance_ids', [])),
                    'status': status,
                })
                continue
            if not write:
                # The report Lambda writes the report. A stopped run whose commands are still
                # going stays under Running now meanwhile (30 Oct live: it dropped out 37 s early).
                if status not in ('Success', 'CompletedWithSuccess') and _ended_within(exec_data, COMMANDS_FINISH_HOURS):
                    finishing = _count_execution_outcomes(ssm, execution_id, _DEFAULT_REGION)
                    if finishing.get("still_running"):
                        running.append(_finishing_entry(execution_id, context, finishing))
                continue

            # Check if final report already exists (idempotent). The run's own start time
            # dates the report; the context's (written when the agent started it) is the fallback.
            started_at = (_coerce_utc_dt(exec_data.get('ExecutionStartTime'))
                          or _coerce_utc_dt(context.get('started_at')) or datetime.now(timezone.utc))

            date_prefix = f"{started_at.year}/{started_at.month:02d}/{started_at.day:02d}/"
            final_key = f"{date_prefix}{execution_id}.json"

            try:
                s3.head_object(Bucket=bucket, Key=final_key)
                # Already exists — just delete the pending file
                s3.delete_object(Bucket=bucket, Key=key)
                logger.info(f"[RECONCILE] Report already exists for {execution_id}, cleaned up pending")
                continue
            except ClientError as e:
                if e.response.get('Error', {}).get('Code') not in ('404', 'NoSuchKey', 'NotFound'):
                    # Throttling or access errors: try again next time rather than guess.
                    logger.warning(f"[RECONCILE] Could not check for report {final_key}: {e}")
                    continue

            # Build the final compliance report
            ended_at = exec_data.get('ExecutionEndTime')
            duration_seconds = None
            completed_at = None  # remediation completion time, for the SLA calc
            if ended_at and started_at:
                ended = ended_at if isinstance(ended_at, datetime) else datetime.fromisoformat(str(ended_at))
                if ended.tzinfo is None:
                    ended = ended.replace(tzinfo=timezone.utc)
                completed_at = ended
                duration_seconds = (ended - started_at).total_seconds()

            # Walk child executions to count actual outcomes. The pending
            # context can't supply these — for tag-based runs SSM only
            # resolves the instance list at execution time. Cross-account
            # children require assume-role into the spoke; the helper
            # handles that.
            outcomes = _count_execution_outcomes(ssm, execution_id, _DEFAULT_REGION)
            # Commands the run sent are still going (a stopped run): the report waits for them,
            # and the run still shows as running, since those instances are still patching.
            if outcomes.get("still_running") and (
                    completed_at is None
                    or datetime.now(timezone.utc) - completed_at < timedelta(hours=COMMANDS_FINISH_HOURS)):
                logger.info(f"[RECONCILE] {execution_id} is {status} but {outcomes['still_running']} "
                            f"command(s) are still running; the report waits for them")
                running.append(_finishing_entry(execution_id, context, outcomes))
                continue
            # Some outcomes couldn't be read: the report waits for a full count while the run is
            # recent, then is written marked incomplete rather than as if the rest never ran.
            incomplete = outcomes.get("incomplete")
            if incomplete and (
                    completed_at is None
                    or datetime.now(timezone.utc) - completed_at < timedelta(hours=COMMANDS_FINISH_HOURS)):
                logger.info(f"[RECONCILE] {execution_id}: outcomes couldn't be read in "
                            f"{', '.join(outcomes['unread_locations'])}; the report waits for them")
                continue

            # Resolve instance_ids: prefer the live list from SendCommand
            # invocations; fall back to the pending context (Path A only).
            # A partial count never replaces the context's list.
            resolved_instance_ids = ((context.get("instance_ids") if incomplete else None)
                                     or outcomes.get("instance_ids") or context.get("instance_ids", []) or [])
            instance_count = (len(resolved_instance_ids) if incomplete and context.get("instance_ids")
                              else outcomes['instance_count'] or len(resolved_instance_ids))

            operation_type = context.get('operation_type', 'patch')
            sla_met_value, sla_reason = _sla_outcome(context, completed_at, status, outcomes)

            if operation_type == 'rollback':
                # Rollback reports: only execution + scope + operator. No
                # vulnerability, compliance, or patch_state sections.
                report = {
                    'report_id': execution_id,
                    'timestamp': started_at.isoformat(),
                    'operation_type': 'rollback',
                    'execution': {
                        'execution_id': execution_id,
                        'status': status,
                        'duration_seconds': duration_seconds,
                        'failure_message': exec_data.get('FailureMessage', ''),
                        'success_count': outcomes['success_count'],
                        'failure_count': outcomes['failure_count'],
                        **_incomplete_fields(outcomes),
                    },
                    'scope': {
                        'environment': context.get('environment', 'unknown'),
                        'targeting': context.get('targeting', 'unknown'),
                        'instance_ids': resolved_instance_ids,
                        'instance_count': instance_count,
                        'account_ids': context.get('account_ids', []),
                        'regions': context.get('regions', []),
                        **_report_tag_scope(context),
                    },
                    'operator': context.get('operator', 'unknown'),
                    'reconciled_at': datetime.now(timezone.utc).isoformat(),
                }
            else:
                # Patch reports (default): full compliance report
                report = {
                    'report_id': execution_id,
                    'timestamp': started_at.isoformat(),
                    'operation_type': 'patch',
                    'execution': {
                        'execution_id': execution_id,
                        'plan_id': context.get('plan_id'),
                        'status': status,
                        'decision': context.get('decision', 'EMERGENCY'),
                        **({'decision_reason': context['decision_reason']} if context.get('decision_reason') else {}),
                        'duration_seconds': duration_seconds,
                        'failure_message': exec_data.get('FailureMessage', ''),
                        'success_count': outcomes['success_count'],
                        'failure_count': outcomes['failure_count'],
                        **_incomplete_fields(outcomes),
                    },
                    'vulnerability': {
                        'cve_id': context.get('cve_id') or 'N/A',
                        'severity': context.get('severity') or 'UNKNOWN',
                        'cvss_score': context.get('cvss_score'),
                        'additional_cve_ids': context.get('additional_cve_ids', []) or [],
                    },
                    'scope': {
                        'environment': context.get('environment', 'unknown'),
                        'targeting': context.get('targeting', 'unknown'),
                        'instance_ids': resolved_instance_ids,
                        'instance_count': instance_count,
                        'account_ids': context.get('account_ids', []),
                        'regions': context.get('regions', []),
                        'severity_filter': context.get('severity_filter'),
                        **_report_tag_scope(context),
                    },
                    'compliance': {
                        'sla_hours': context.get('sla_hours'),
                        'sla_source': context.get('sla_source'),
                        'first_observed_at': context.get('first_observed_at'),
                        'sla_met': sla_met_value,
                        'sla_reason': sla_reason,
                        'frameworks': context.get('frameworks') or [],
                    },
                    'patch_state': {
                        'pre_patch': context.get('pre_patch_state'),
                        'post_patch': context.get('post_patch_state'),
                    },
                    'install_packages': context.get('install_packages') or [],
                    'reboot': {
                        'option': context.get('reboot_option', 'NoReboot'),
                        'pending_instance_ids': _pending_reboots(
                            resolved_instance_ids, context.get('account_ids', []), context.get('regions', [])),
                    },
                    'operator': context.get('operator', 'unknown'),
                    'reconciled_at': datetime.now(timezone.utc).isoformat(),
                }

            # Write final report.
            #
            # S3 object Metadata mirrors the JSON body's key audit fields so
            # query_compliance_reports (compliance analyst tool) can use the
            # cheap head_object path instead of fetching every body. Field
            # names follow the SSM Resource Data Sync convention (kebab-case).
            frameworks_list = context.get("frameworks") or []
            metadata: dict[str, str] = {
                "cve-id": (context.get("cve_id") or "N/A"),
                "severity": (context.get("severity") or "UNKNOWN"),
                "environment": context.get("environment", "unknown"),
                "decision-type": context.get("decision", "EMERGENCY"),
                "sla-met": sla_met_value,
                # S3 metadata values are flat strings; pack the frameworks
                # list as a comma-separated string for cheap head_object reads.
                "frameworks": ",".join(frameworks_list) if frameworks_list else "",
                **_tag_metadata(context),
                "operator": context.get("operator", "unknown"),
            }
            if report.get("reboot"):
                metadata["reboot-pending"] = str(len(report["reboot"]["pending_instance_ids"]))
            # S3 metadata values must be ASCII strings; coerce defensively.
            metadata = {k: str(v)[:1024] for k, v in metadata.items() if v is not None}

            try:
                s3.put_object(
                    Bucket=bucket,
                    Key=final_key,
                    Body=json.dumps(report, default=str).encode("utf-8"),
                    ContentType="application/json",
                    Metadata=metadata,
                    IfNoneMatch="*",
                )
            except ClientError as e:
                if e.response.get('Error', {}).get('Code') not in ('PreconditionFailed', 'ConditionalRequestConflict'):
                    raise
                logger.info(f"[RECONCILE] Report for {execution_id} was written by another reconciler")

            # Delete pending file
            s3.delete_object(Bucket=bucket, Key=key)
            generated_count += 1
            logger.info(f"[RECONCILE] Generated report for {execution_id} (status={status})")

        except Exception as e:
            logger.warning(f"[RECONCILE] Failed to process pending {key}: {e}")
            # A file that keeps failing is dropped once it's past the age limit, as an
            # unreadable execution is, so it isn't retried on every sweep for ever.
            written = _coerce_utc_dt((context or {}).get('started_at')) or obj.get('LastModified')
            if write and written and datetime.now(timezone.utc) - written > timedelta(days=PENDING_MAX_AGE_DAYS):
                try:
                    s3.delete_object(Bucket=bucket, Key=key)
                    logger.warning(f"[RECONCILE] Dropped pending {execution_id}: still failing after "
                                   f"{PENDING_MAX_AGE_DAYS} days ({e})")
                except Exception as de:
                    logger.warning(f"[RECONCILE] Could not drop pending {key}: {de}")
            continue

    if generated_count > 0:
        logger.info(f"[RECONCILE] Generated {generated_count} compliance report(s)")
    return generated_count, running


def _coerce_utc_dt(value) -> "datetime | None":
    """Parse an ISO-8601 string or datetime into a tz-aware UTC datetime.

    Returns None when the value is missing or unparseable. Naive datetimes are
    assumed to be UTC.
    """
    if value is None:
        return None
    dt = value if isinstance(value, datetime) else None
    if dt is None:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # boto returns times in the machine's zone; reports are filed and sorted in UTC (2 Nov: a
    # report written at +11:00 sorted above later ones, and its date prefix was local).
    return dt.astimezone(timezone.utc)


def _incomplete_fields(outcomes: dict) -> dict:
    """The report's execution fields for outcomes that couldn't all be read: the flag and where."""
    if not outcomes.get("incomplete"):
        return {}
    return {"incomplete": True, "unread_locations": list(outcomes.get("unread_locations") or [])}


def _sla_outcome(context: dict, completed_at, status: str, outcomes: dict) -> "tuple":
    """SLA result for a run, as (sla_met, reason).

    sla_met is True or False only when the run succeeded on at least one
    instance with no failures; otherwise it's None with the reason. A failed,
    cancelled or empty run proves neither remediation nor a breach.
    """
    if status != "Success":
        return None, f"run ended {status}"
    if outcomes.get("incomplete"):
        return None, f"outcomes couldn't be read in {', '.join(outcomes.get('unread_locations') or [])}"
    failures = outcomes.get("failure_count") or 0
    if failures:
        return None, f"{failures} instance(s) failed"
    if not outcomes.get("instance_count"):
        return None, "no instances were patched"
    met = _evaluate_sla(context.get("sla_hours"), context.get("first_observed_at"), completed_at)
    if met is None:
        return None, "SLA or discovery time not recorded"
    return met, None


def _evaluate_sla(sla_hours, first_observed_at, remediation_completed_at) -> "bool | None":
    """Was the vulnerability remediated within its SLA window?

    Compliance measures the elapsed time from when the vulnerability was first
    observed (Inspector's firstObservedAt) to when remediation completed,
    against sla_hours. Returns a real bool so downstream `is True`/`is False`
    checks behave, or None when it cannot be determined: no sla_hours, no
    discovery timestamp, no completion time, or timestamps that fail to parse
    or run backwards.

    None must never read as met. The earlier implementation compared the patch
    job's *duration* against the SLA window — unrelated to compliance, and true
    for any fast job. "Unknown" is the honest answer until a discovery
    timestamp (first_observed_at) is captured and forwarded in the context.
    """
    if sla_hours is None:
        return None
    observed = _coerce_utc_dt(first_observed_at)
    completed = _coerce_utc_dt(remediation_completed_at)
    if observed is None or completed is None:
        return None
    elapsed_seconds = (completed - observed).total_seconds()
    if elapsed_seconds < 0:
        return None  # inconsistent timestamps — don't guess
    return elapsed_seconds <= sla_hours * 3600


# ── Run status ──────────────────────────────────────────────────────

# Every Automation status that isn't final: a report written in one of these
# would record a run that hasn't ended.
_RUN_ACTIVE = {"Pending", "InProgress", "Waiting", "Cancelling", "PendingApproval", "Approved", "Scheduled",
               "RunbookInProgress", "PendingChangeCalendarOverride", "ChangeCalendarOverrideApproved"}


def is_active(status: str) -> bool:
    """Whether an Automation status means the run hasn't ended."""
    return status in _RUN_ACTIVE
_RUN_FAILED = {"Failed", "TimedOut", "Cancelled", "CompletedWithFailure", "Rejected"}


def _iso(dt) -> str | None:
    return dt.isoformat() if hasattr(dt, "isoformat") else None


def _location_status(step: dict) -> "str | None":
    """A location step's status. SSM can keep an instance-ID run's location step Pending while
    its child patches (disruption round, 2 Nov); one with a start time has started."""
    status = step.get("StepStatus")
    if status in ("Pending", "Waiting") and step.get("ExecutionStartTime"):
        return "InProgress"
    return status


def run_summary(ssm, execution_id: str) -> "dict | None":
    """Overall and per-(account, region) status of a Patchy run, from the parent's steps.

    With TargetLocations the parent lists one step per location ("<account>_<region>");
    returns None for anything that isn't a Patchy runbook.
    """
    ex = ssm.get_automation_execution(AutomationExecutionId=execution_id)["AutomationExecution"]
    if not str(ex.get("DocumentName", "")).startswith("Patchy-"):
        return None
    steps = list(ex.get("StepExecutions") or [])
    if ex.get("StepExecutionsTruncated"):
        steps = []
        for page in ssm.get_paginator("describe_automation_step_executions").paginate(
                AutomationExecutionId=execution_id):
            steps.extend(page.get("StepExecutions") or [])
    children = []
    for st in steps:
        account, _, region = str(st.get("StepName", "")).partition("_")
        children.append({"account": account, "region": region, "status": _location_status(st),
                         "started_at": _iso(st.get("ExecutionStartTime")),
                         "ended_at": _iso(st.get("ExecutionEndTime"))})
    status = ex.get("AutomationExecutionStatus")
    return {
        "execution_id": execution_id, "document": ex.get("DocumentName"), "status": status,
        "active": status in _RUN_ACTIVE, "started_at": _iso(ex.get("ExecutionStartTime")),
        "ended_at": _iso(ex.get("ExecutionEndTime")), "children": children,
        "counts": {"total": len(children),
                   "succeeded": sum(1 for c in children if c["status"] == "Success"),
                   "failed": sum(1 for c in children if c["status"] in _RUN_FAILED),
                   "running": sum(1 for c in children if c["status"] in _RUN_ACTIVE)},
    }
