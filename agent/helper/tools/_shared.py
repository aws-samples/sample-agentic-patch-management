"""Shared state, AWS client helpers, validation functions, and caching infrastructure.

This module contains all cross-cutting utilities that multiple tool modules
depend on. It is the single source of truth for:

  - AWS client factories (_ec2, _ssm, _s3, _inspector, _cloudwatch)
  - Constants (scope tags, document names, execution defaults, regions)
  - ContextVars for operator identity, timezone, and per-request state
  - Instance ID validation and scope-tag verification
  - Fleet cache infrastructure (Explorer + direct EC2 describe hybrid)
  - Instance grouping by (account, region) for MAMR Automations
  - SLA resolution from instance tags
  - Baseline override URL resolution
  - S3 compliance bucket helpers
  - Automation starters (tag-based and instance-ID-based)
  - Error classification (re-exported from helper.error_handling)
  - Environment normalization
  - UTC-to-local time formatting

Tool modules import from here:
    from ._shared import (
        _ssm, _ec2, _s3, _inspector, _cloudwatch,
        _validate_instance_ids, _validate_instance_scope,
        _group_instances_by_location, ...
    )
"""

# ============================================================================
# IMPORTS
# ============================================================================

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import contextlib
import contextvars
from helper.concurrency import ContextThreadPoolExecutor as ThreadPoolExecutor
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid

from helper.error_handling import classify_error
from helper.cross_account import (
    is_multi_account, get_client, get_hub_account_id,
    resolve_scope,
    EXECUTION_DEFAULTS, SPOKE_EXECUTION_ROLE, SPOKE_REGIONS, execution_limits,
    REBOOT_OPTIONS, reboot_option,
)

# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================================
# CONSTANTS
# ============================================================================

# Region from environment variable (default: us-east-1)
AWS_REGION = os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))

# Instance ID validation pattern
INSTANCE_ID_PATTERN = re.compile(r'^i-[0-9a-f]{8,17}$')

# Scope tags
SCOPE_TAG_KEY = os.environ.get('SSM_SCOPE_TAG_KEY', 'PatchAutomation')
SCOPE_TAG_VALUE = os.environ.get('SSM_SCOPE_TAG_VALUE', 'enabled')

# Automation document names deployed by Patchy-SsmDocs and the StackSet. Fixed, not settings:
# the IAM statements and the report rule match the Patchy- prefix.
AUTOMATION_DOC_NAME = 'Patchy-RunPatchBaseline'
AUTOMATION_BY_ID_DOC_NAME = 'Patchy-RunPatchBaselineById'
ROLLBACK_DOC_NAME = 'Patchy-RunRollback'
ROLLBACK_BY_ID_DOC_NAME = 'Patchy-RunRollbackById'

# Explorer configuration
EXPLORER_SYNC_NAME = 'patchy-fleet-sync'
MAX_FLEET_SIZE = int(os.environ.get('MAX_FLEET_SIZE', '5000'))

# Spoke account discovery from environment
SPOKE_ACCOUNT_IDS_ENV = [a.strip() for a in os.environ.get('SPOKE_ACCOUNT_IDS', '').split(',') if a.strip()]
SPOKE_OU_IDS_ENV = [o.strip() for o in os.environ.get('SPOKE_OU_IDS', '').split(',') if o.strip()]

# Default SLA hours by severity (used when instances don't have SLA tags)
_DEFAULT_SLA = {
    'CRITICAL': 24,
    'HIGH': 72,
    'MEDIUM': 168,
    'LOW': 720,
}

# Severity -> baseline override S3 key mapping
_SEVERITY_OVERRIDE_MAP = {
    'CRITICAL': 'baseline-overrides/critical-only.json',
    'HIGH': 'baseline-overrides/high-and-above.json',
    'IMPORTANT': 'baseline-overrides/high-and-above.json',  # SSM uses "Important" for High
    'MEDIUM': 'baseline-overrides/medium-and-above.json',
    'LOW': 'baseline-overrides/all-severities.json',
}

# ============================================================================
# AWS CLIENT CONFIGURATION
# ============================================================================

# AWS client configuration with retry logic and timeouts
aws_config = Config(
    retries={
        'max_attempts': 3,
        'mode': 'adaptive'  # Adaptive retry mode for better handling of throttling
    },
    read_timeout=180,
    connect_timeout=10
)

# ============================================================================
# CONTEXT VARIABLES
# ============================================================================

# Per-request operator identity -- safe for concurrent async requests
_current_operator: contextvars.ContextVar[str] = contextvars.ContextVar('_current_operator', default='unknown')
_current_timezone: contextvars.ContextVar[str] = contextvars.ContextVar('_current_timezone', default='UTC')
# Conversation the current request belongs to -- stored plans are bound to it.
_current_session: contextvars.ContextVar[str] = contextvars.ContextVar('_current_session', default='')
# The operator's message this turn -- lets plan tools honour what was asked when the model omits it.
_current_message: contextvars.ContextVar[str] = contextvars.ContextVar('_current_message', default='')
# Scans created this request -- blocks execute in same invocation as dry-run.
_current_request_scans: contextvars.ContextVar[set | None] = contextvars.ContextVar('_current_request_scans', default=None)
# Per-request instance cache -- avoids redundant EC2 describe_instances calls within a single agent invocation.
# Parallel AWS reads across (account, region) pairs.
_FANOUT_WORKERS = 8
# Without a filter, GetOpsSummary pages over every OpsData record in the sync and
# returns mostly empty pages (36 calls for 10 instances on a test fleet). With it, one.
EXPLORER_INSTANCE_FILTER = [{'Key': 'AWS:EC2InstanceInformation.InstanceId', 'Values': ['i-'], 'Type': 'BeginWith'}]
# EC2 accepts at most 200 values per filter.
_EC2_FILTER_VALUES = 200

_request_instance_cache: contextvars.ContextVar[Optional[Dict[str, dict]]] = contextvars.ContextVar('_request_instance_cache', default=None)
# SSM reads for maintenance windows, kept for one request: one turn often asks
# about the same windows several times (windows, forecast, SLA check).
_request_window_cache: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar('_request_window_cache', default=None)


def request_cached(key: tuple, fetch):
    """fetch() once per request for this key; outside a request, every call fetches."""
    cache = _request_window_cache.get()
    if cache is None:
        return fetch()
    if key not in cache:
        cache[key] = fetch()
    return cache[key]


def forget_request_cache() -> None:
    """Drop the request's cached reads, e.g. after creating a window in this turn."""
    cache = _request_window_cache.get()
    if cache:
        cache.clear()

# ============================================================================
# OPERATOR / TIMEZONE CONTEXT
# ============================================================================


def set_operator(identity: str) -> None:
    """Set the operator identity for the current request context."""
    _current_operator.set(identity)


def get_operator() -> str:
    """Get the operator identity for the current request context."""
    return _current_operator.get()


def set_session_id(session_id: str) -> None:
    """Set the conversation session for the current request context."""
    _current_session.set(session_id or '')


def get_session_id() -> str:
    """Get the conversation session for the current request context."""
    return _current_session.get()


def set_operator_message(text: str) -> None:
    """Set the operator's message for the current request context."""
    _current_message.set(text or '')


def get_operator_message() -> str:
    """Get the operator's message for the current request context."""
    return _current_message.get()


def set_timezone(tz_name: str) -> None:
    """Set the user's timezone for the current request context."""
    _current_timezone.set(tz_name or 'UTC')


def get_timezone() -> str:
    """Get the user's timezone for the current request context."""
    return _current_timezone.get()


def clear_request_scans() -> None:
    """Reset scan tracking and the request's window cache at the start of each request."""
    _current_request_scans.set(set())
    _request_window_cache.set({})


# ============================================================================
# TIME FORMATTING
# ============================================================================


def _format_utc_as_local(utc_str: str) -> str:
    """Convert a UTC datetime string to the user's local timezone for display.

    Returns a human-readable string like '24 Feb 2026 12:00 PM AEST'.
    Falls back to UTC if timezone conversion fails.
    """
    try:
        from zoneinfo import ZoneInfo
        tz_name = get_timezone()
        # Parse the UTC time
        utc_clean = str(utc_str).replace('Z', '+00:00')
        utc_dt = datetime.fromisoformat(utc_clean)
        if utc_dt.tzinfo is None:
            utc_dt = utc_dt.replace(tzinfo=timezone.utc)
        # Convert to local
        local_tz = ZoneInfo(tz_name)
        local_dt = utc_dt.astimezone(local_tz)
        # Format nicely
        return local_dt.strftime('%d %b %Y %I:%M %p %Z')
    except Exception:
        return str(utc_str)


# ============================================================================
# AWS CLIENT FACTORIES
# ============================================================================


def _ssm(account_id=None, region=None):
    return get_client('ssm', account_id, region)


def _ec2(account_id=None, region=None):
    return get_client('ec2', account_id, region)


def _inspector(account_id=None, region=None):
    return get_client('inspector2', account_id, region)


def _cloudwatch(account_id=None, region=None):
    return get_client('cloudwatch', account_id, region)


def _s3(account_id=None, region=None):
    return get_client('s3', account_id, region)


# ============================================================================
# VALIDATION HELPERS
# ============================================================================


def _validate_instance_ids(instance_ids: List[str]) -> Optional[dict]:
    """Returns error dict if any ID is invalid, None if all valid."""
    if not instance_ids:
        return {
            "error": "instance_ids list is empty",
            "error_code": "InvalidParameter",
            "category": "ABORT",
            "retryable": False
        }
    for iid in instance_ids:
        if not INSTANCE_ID_PATTERN.match(iid):
            return {
                "error": f"Invalid instance ID: {iid}",
                "error_code": "InvalidInstanceId",
                "category": "ABORT",
                "retryable": False
            }
    return None


def _instance_environments(instance_ids: List[str]) -> Dict[str, Optional[str]]:
    """Normalised Environment tag per instance, None when the tag is missing.

    Args:
        instance_ids: Instances to look up (already scope-validated).

    Returns:
        {instance_id: 'dev' | 'staging' | 'prod' | other | None}
    """
    described = _direct_describe_scoped_instances(instance_ids=instance_ids)
    envs: Dict[str, Optional[str]] = {}
    for iid in instance_ids:
        tags = (described.get(iid) or {}).get('tags') or {}
        raw = next((v for k, v in tags.items() if k.lower() == 'environment'), None)
        envs[iid] = _normalize_environment(raw) if raw else None
    return envs


_TAG_KEY = re.compile(r'^[\w.:/=+@-][\w .:/=+@-]{0,127}$')


def report_tag_keys() -> List[str]:
    """Instance tags recorded on every compliance report (REPORT_TAGS, default Team,Product)."""
    raw = os.environ.get('REPORT_TAGS')
    names = ['Team', 'Product'] if raw is None else [n.strip() for n in raw.split(',')]
    out: List[str] = []
    for n in names:
        if n and _TAG_KEY.match(n) and n.lower() not in {o.lower() for o in out}:
            out.append(n)
    return out[:10]


def _pick_report_tags(tags: Dict[str, str]) -> Dict[str, str]:
    """The REPORT_TAGS values of one instance, matched case-insensitively, under the configured spelling."""
    by_lower = {k.lower(): v for k, v in (tags or {}).items()}
    return {k: by_lower[k.lower()] for k in report_tag_keys() if by_lower.get(k.lower())}


def _report_tags(context: Dict[str, Any]) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    """(tags, mixed values) for a run's report: each REPORT_TAGS tag's value across the run's instances.

    A tag with one value across the instances records that value; several give
    "mixed", with the values listed; none gives "unknown". An instance-ID run uses its
    instances; a tag-based run uses the scoped instances of its environment in its
    accounts and Regions, as the fleet view sees them when the run starts.
    """
    keys = report_tag_keys()
    if not keys:
        return {}, {}
    _get_fleet_summary()
    cache = _fleet_instances_cache or {}
    if context.get('instance_ids'):
        ids = list(context['instance_ids'])
        missing = [i for i in ids if i not in cache]
        described = _direct_describe_scoped_instances(instance_ids=missing) if missing else {}
        per_instance = [cache[i].get('report_tags') or {} if i in cache
                        else _pick_report_tags((described.get(i) or {}).get('tags') or {}) for i in ids]
    else:
        env = _normalize_environment(context.get('environment') or '')
        accounts, regions = set(context.get('account_ids') or []), set(context.get('regions') or [])
        per_instance = [info.get('report_tags') or {} for info in cache.values()
                        if info.get('managed') and _normalize_environment(info.get('environment') or '') == env
                        and (not accounts or info.get('account_id') in accounts)
                        and (not regions or info.get('region') in regions)]
    tags: Dict[str, str] = {}
    values: Dict[str, List[str]] = {}
    for key in keys:
        seen = sorted({t[key] for t in per_instance if t.get(key)})
        if not seen:
            tags[key] = 'unknown'
        elif len(seen) == 1:
            tags[key] = seen[0]
        else:
            tags[key], values[key] = 'mixed', seen
    return tags, values


def fleet_instances() -> Dict[str, dict]:
    """The fleet cache as last loaded by _get_fleet_summary: {instance_id: info}."""
    return _fleet_instances_cache or {}


def fleet_view_error() -> Optional[str]:
    """The error code from the last fleet refresh, when SSM Explorer couldn't be read; else None."""
    return _fleet_view_error


def fleet_view_unreadable() -> List[str]:
    """Locations ("account/region") whose instances couldn't be read on the last fleet refresh."""
    return list(_fleet_view_unreadable)


def fleet_view_truncated() -> bool:
    """Whether the cached fleet view stopped at MAX_FLEET_SIZE with instances left unread."""
    return _fleet_view_truncated


def _environment_tag_values(environment: str, account_ids: Optional[List[str]] = None) -> List[str]:
    """The Environment tag values scoped instances in an environment actually carry.

    The tag-based runbooks match tag:Environment exactly, so "prod" doesn't
    reach instances tagged "Production" even though Patchy treats them alike.
    """
    # The same instances a plan targets: the fleet view and those EC2 reports before
    # Explorer does, so the run reaches what was approved (2 Nov review, L1).
    return sorted({v['environment_tag'] for v in environment_instances(environment, account_ids).values()
                   if v.get('environment_tag')})


def _validate_instance_scope(instance_ids: List[str], fresh: bool = False) -> Optional[dict]:
    """Returns error dict if any instance is missing the scope tag, None if all in scope.

    fresh: when a plan starts, confirm every instance's scope tag in EC2 now rather
    than trusting a fleet view up to five minutes old, so an instance whose owner
    has just removed the tag isn't patched (2 Nov review, L3).

    Resolution order, per instance:
      1. Explorer cache (`_fleet_instances_cache`) -- fast path.
      2. Direct EC2 describe across configured (account, region) pairs with the
         scope tag filter -- covers instances Explorer hasn't ingested yet
         (30 min to ~1 day lag, up to ~6 h on first sync).
      3. If direct describe returns empty but instance IS in the fleet cache
         (unmanaged/pending), retry once -- covers transient assume-role failures.
      4. Marked out-of-scope only when EC2 itself can't find a tagged instance.

    The previous implementation only fell back to a hub-account EC2 describe,
    so spoke-account instances stuck behind Explorer's ingestion lag were
    falsely rejected as out-of-scope, blocking patch operations against real
    tagged instances.
    """
    try:
        # Try Explorer cache first (covers cross-account instances)
        _get_fleet_summary()
        located: Dict[str, Tuple[str, str]] = {}
        if _fleet_instances_cache and not fresh:
            in_scope = {iid for iid in instance_ids
                       if iid in _fleet_instances_cache and _fleet_instances_cache[iid].get('managed')}
            for iid in in_scope:
                c = _fleet_instances_cache[iid]
                located[iid] = (c.get('account_id'), c.get('region'))
        else:
            in_scope = set()

        # Fallback: direct EC2 describe across the full configured scope
        # (hub + spokes x SPOKE_REGIONS) for any instance not in the Explorer
        # cache. The describe applies the SCOPE_TAG filter at the API level
        # so untagged instances never enter `in_scope`.
        unchecked = [iid for iid in instance_ids if iid not in in_scope]
        unread_locations: List[str] = []
        if unchecked:
            described = _direct_describe_scoped_instances(instance_ids=unchecked)
            unread_locations = list(getattr(described, 'unreadable', []) or [])
            for iid, info in described.items():
                in_scope.add(iid)
                located[iid] = (info.get('account_id'), info.get('region'))

        # Retry logic: if instances are known to the fleet cache (even as
        # unmanaged/pending) but direct describe didn't find them, it likely
        # means a transient assume-role failure silently skipped that account.
        # Retry once for those specific instances.
        still_missing = [iid for iid in instance_ids if iid not in in_scope]
        if still_missing and _fleet_instances_cache:
            known_but_missing = [iid for iid in still_missing
                                 if iid in _fleet_instances_cache]
            if known_but_missing:
                logger.warning(f"[SCOPE_VALIDATION] {len(known_but_missing)} instance(s) in fleet "
                               f"cache but not found by direct describe; retrying: {known_but_missing[:3]}")
                # Retry with explicit accounts/regions from cache metadata
                for iid in known_but_missing:
                    cached = _fleet_instances_cache[iid]
                    retry_acct = cached.get('account_id')
                    retry_region = cached.get('region')
                    if retry_acct and retry_region:
                        retry_result = _direct_describe_scoped_instances(
                            instance_ids=[iid],
                            accounts=[retry_acct],
                            regions=[retry_region],
                        )
                        if iid in retry_result:
                            in_scope.add(iid)
                            located[iid] = (retry_acct, retry_region)

        # Every path, the Explorer cache included, must land inside the
        # configured accounts and regions.
        for iid in list(in_scope):
            account, region = located.get(iid, (None, None))
            if region not in SPOKE_REGIONS or not account or _validate_account_scope([account]):
                logger.warning(f"[SCOPE_VALIDATION] {iid} is in {account}/{region}, outside the configured scope")
                in_scope.discard(iid)

        out_of_scope = [iid for iid in instance_ids if iid not in in_scope]
        if out_of_scope and unread_locations:
            # Not found, but some locations couldn't be read: not proof they're out of scope
            # (2 Nov review, H L3). Still refused, since scope wasn't confirmed.
            return {
                "error": f"{len(out_of_scope)} instance(s) weren't found, and {', '.join(unread_locations[:3])} "
                         "couldn't be read, so Patchy can't confirm they're in scope.",
                "error_code": "ScopeUnreadable",
                "category": "ABORT",
                "retryable": True,
                "out_of_scope_instances": out_of_scope,
                "unreadable_locations": unread_locations,
            }
        if out_of_scope:
            return {
                "error": f"{len(out_of_scope)} instance(s) not found in any configured "
                         f"account/region with required tag {SCOPE_TAG_KEY}={SCOPE_TAG_VALUE}: {out_of_scope[:5]}. "
                         "Causes: invalid instance ID, missing scope tag, instance "
                         "terminated, or spoke role not deployed.",
                "error_code": "InstanceOutOfScope",
                "category": "ABORT",
                "retryable": False,
                "out_of_scope_instances": out_of_scope,
            }
    except ClientError as e:
        # Fail closed: a scope check that couldn't run doesn't allow a change.
        logger.warning(f"Scope validation failed: {e}")
        return {"error": f"Couldn't confirm the instances are in scope: {e}", "error_code": "ScopeCheckFailed",
                "category": "ABORT", "retryable": True}
    return None


def _validate_account_scope(account_ids: List[str]) -> Optional[dict]:
    """Returns an error dict if any account is outside the configured scope, None otherwise.

    The tag-based write paths (multi_account_execute / multi_account_rollback)
    take account_ids directly rather than deriving them from scope-validated
    instances, so they need their own account check — the read paths already
    filter to _get_configured_scope_accounts(). With no accounts configured,
    single-account mode allows only the hub and multi-account mode allows
    nothing, so a missing setting can't widen the scope to every account.
    """
    allowed = _get_configured_scope_accounts()
    if not allowed:
        allowed = set() if is_multi_account() else {get_hub_account_id()}
    out_of_scope = [a for a in (account_ids or []) if a not in allowed]
    if out_of_scope:
        return {
            "error": f"{len(out_of_scope)} account(s) outside the configured patch scope: "
                     f"{out_of_scope}. Allowed accounts come from SPOKE_ACCOUNT_IDS or "
                     "SPOKE_OU_IDS (plus the hub). Call resolve_execution_scope to get valid accounts.",
            "error_code": "AccountOutOfScope",
            "category": "ABORT",
            "retryable": False,
            "result_type": "error",
            "out_of_scope_accounts": out_of_scope,
            "next_action": "The operation was NOT started. Present this to the operator.",
        }
    return None


def _validate_regions(regions: Optional[List[str]]) -> Optional[dict]:
    """Returns an error dict if any Region isn't one of SPOKE_REGIONS, None otherwise."""
    outside = [r for r in (regions or []) if r not in SPOKE_REGIONS]
    if outside:
        return {
            "error": f"Region(s) {outside} are outside the configured Regions {SPOKE_REGIONS}.",
            "error_code": "RegionOutOfScope",
            "category": "ABORT",
            "retryable": False,
            "result_type": "error",
            "next_action": "The operation was NOT started. Use the configured Regions or omit regions.",
        }
    return None


def _normalize_environment(user_input: str) -> str:
    """Map user input to canonical environment tag value.

    Handles common aliases so users can say 'production' and it maps to 'prod'.
    """
    aliases = {
        'dev': ['dev', 'development', 'non prod', 'non production'],
        'test': ['test', 'testing', 'qa'],
        'staging': ['staging', 'stage', 'uat'],
        'prod': ['prod', 'production'],
    }
    user_lower = user_input.lower().strip()
    for canonical, alias_list in aliases.items():
        if user_lower in alias_list:
            return canonical
    return user_input


# ============================================================================
# CROSS-ACCOUNT / CROSS-REGION QUERY TARGETS
# ============================================================================
#
# When the agent answers discovery questions (vulnerabilities, patch compliance),
# it must query every (account, region) pair the operator has configured. The
# pair list is derived from:
#
#   - The hub account (always -- local credentials)
#   - SPOKE_ACCOUNT_IDS (or organisations discovery if not set)
#   - Crossed with SPOKE_REGIONS
#
# Single-account / single-region setups still work: the list collapses to one
# pair (hub x AWS_REGION).

# Process-lifetime cache for configured scope. Resolution can hit Organizations
# (ListAccountsForParent) for OU expansion, which is expensive. Org membership
# changes rarely; if an account is added to an OU mid-session the agent can be
# bounced to pick it up.
_configured_scope_cache: Optional[set] = None
_configured_scope_expires = 0.0
# Organization membership changes rarely, but a moved account should count within minutes.
SCOPE_CACHE_SECONDS = 900


def _resolve_ou_member_accounts(ou_ids: List[str]) -> List[str]:
    """Resolve OU IDs to active member account IDs via Organizations.

    Returns None (and logs) on failure, so the caller can tell a failed lookup
    from an OU with no accounts. The hub account is included if it's in one of
    the listed OUs.
    """
    if not ou_ids:
        return []
    try:
        from helper.cross_account import discover_accounts
        # discover_accounts excludes the hub by default -- we want the hub back IF it's
        # genuinely in one of the listed OUs. Caller will union with the hub anyway,
        # so excluding it here is harmless.
        members = discover_accounts(ou_ids=ou_ids)
        return [m['account_id'] for m in members]
    except Exception as e:
        logger.warning(f"[CROSS_REGION] Could not resolve OU members for {ou_ids}: {e}")
        return None


_scope_lock = threading.Lock()


def _get_configured_scope_accounts() -> set:
    """The configured scope (see _resolve_configured_scope), resolved by one caller at a time."""
    if _configured_scope_cache is not None and time.time() < _configured_scope_expires:
        return _configured_scope_cache
    with _scope_lock:
        # Another caller may have resolved it while this one waited.
        if _configured_scope_cache is not None and time.time() < _configured_scope_expires:
            return _configured_scope_cache
        return _resolve_configured_scope()


def _resolve_configured_scope() -> set:
    """Return the operator's configured scope (account allowlist) for filtering.

    Precedence (Option C -- OUs win):
      1. SPOKE_OU_IDS set -> resolve OU members + hub.
      2. SPOKE_OU_IDS unset, SPOKE_ACCOUNT_IDS set -> use that list + hub.
      3. Both unset -> empty set (sentinel meaning "no filter").

    The empty-set sentinel preserves backward compatibility: code that calls
    this function should treat an empty result as "don't filter, include
    everything Inspector/Explorer returns."

    Cached for SCOPE_CACHE_SECONDS. A failed OU lookup gives the hub only
    (refusing changes elsewhere rather than allowing them) and isn't cached,
    so the next call tries again.
    """
    global _configured_scope_cache, _configured_scope_expires, _last_good_scope, _scope_unresolved
    if _configured_scope_cache is not None and time.time() < _configured_scope_expires:
        return _configured_scope_cache

    if not is_multi_account():
        # Single-account mode -- scope is just the hub. Return as a set so the
        # filter logic still applies (drops anything Inspector returns from
        # other accounts, even though that shouldn't happen here).
        try:
            _configured_scope_cache = {get_hub_account_id()}
        except Exception:
            return set()  # not cached: try again next call
        _configured_scope_expires = time.time() + SCOPE_CACHE_SECONDS
        return _configured_scope_cache

    try:
        hub_id = get_hub_account_id()
    except Exception:
        hub_id = None

    if SPOKE_OU_IDS_ENV:
        members = _resolve_ou_member_accounts(SPOKE_OU_IDS_ENV)
        if members is None:
            # Organizations throttles OU walks. The last scope resolved in the past hour is
            # still the best answer; with none, the hub only, which refuses changes elsewhere.
            # Either way reads say the scope couldn't be resolved (2 Nov review, M1).
            _scope_unresolved = True
            at, last = _last_good_scope
            if last and time.time() - at < LAST_GOOD_SCOPE_SECONDS:
                logger.warning(f"[CROSS_REGION] OU lookup failed; using the scope resolved {int(time.time() - at)} s ago")
                return last
            return {hub_id} if hub_id else set()
        scope = set(members) | ({hub_id} if hub_id else set())
        logger.info(f"[CROSS_REGION] Configured scope (OU): {len(scope)} accounts from "
                    f"OUs={SPOKE_OU_IDS_ENV} (members={len(members)}, hub={'yes' if hub_id else 'no'})")
    elif SPOKE_ACCOUNT_IDS_ENV:
        scope = set(SPOKE_ACCOUNT_IDS_ENV) | ({hub_id} if hub_id else set())
        logger.info(f"[CROSS_REGION] Configured scope (explicit): {len(scope)} accounts "
                    f"(spokes={len(SPOKE_ACCOUNT_IDS_ENV)}, hub={'yes' if hub_id else 'no'})")
    else:
        scope = set()
        logger.info("[CROSS_REGION] No SPOKE_OU_IDS or SPOKE_ACCOUNT_IDS configured — "
                    "scope filter disabled (org-wide visibility from Path A APIs)")

    _configured_scope_cache = scope
    _configured_scope_expires = time.time() + SCOPE_CACHE_SECONDS
    _last_good_scope = (time.time(), scope)
    _scope_unresolved = False
    return scope


LAST_GOOD_SCOPE_SECONDS = 3600
_last_good_scope: tuple = (0.0, None)
_scope_unresolved = False


def scope_unresolved() -> bool:
    """Whether the last scope lookup failed, so results filtered by scope may leave accounts out."""
    return _scope_unresolved


def _get_spoke_accounts() -> List[str]:
    """Return spoke account IDs (excluding the hub).

    Precedence (Option C -- OUs win):
      1. SPOKE_OU_IDS set -> resolve OU members.
      2. SPOKE_OU_IDS unset, SPOKE_ACCOUNT_IDS set -> use that list.
      3. Both unset -> Organizations discovery (all active accounts excluding hub).

    Returns [] when MULTI_ACCOUNT_ENABLED is false. Hub is always excluded -- it's
    queried separately with local credentials.
    """
    if not is_multi_account():
        return []
    try:
        hub_id = get_hub_account_id()
    except Exception:
        hub_id = None
    if SPOKE_OU_IDS_ENV:
        # The cached scope: a failed OU lookup gives the hub only, so no spokes, instead of a crash.
        return sorted(a for a in _get_configured_scope_accounts() if a and a != hub_id)
    if SPOKE_ACCOUNT_IDS_ENV:
        return list(dict.fromkeys(a for a in SPOKE_ACCOUNT_IDS_ENV if a and a != hub_id))
    # Defer Organizations discovery to cross_account.discover_accounts (already excludes hub)
    try:
        from helper.cross_account import discover_accounts
        return [a['account_id'] for a in discover_accounts()]
    except Exception as e:
        logger.warning(f"[CROSS_REGION] Could not discover spoke accounts: {e}")
        return []


def _get_query_targets() -> List[tuple]:
    """Return the list of (account_id, region) pairs to query for discovery tools.

    Includes the hub x every region in SPOKE_REGIONS, then each spoke x every
    region in SPOKE_REGIONS. Deduplicated, hub first, regions in SPOKE_REGIONS order.
    """
    try:
        hub_id = get_hub_account_id()
    except Exception:
        # Cannot resolve hub account -- use None so get_client falls back to
        # local credentials (which is the hub). Do NOT use AWS_REGION as an
        # account_id -- that's a region string, not an account.
        hub_id = None
    targets: List[tuple] = []
    seen: set = set()
    if hub_id:
        for rgn in SPOKE_REGIONS:
            key = (hub_id, rgn)
            if key not in seen:
                seen.add(key)
                targets.append(key)
    else:
        # Hub account unknown -- still query each region with None (local creds)
        for rgn in SPOKE_REGIONS:
            key = (None, rgn)
            if key not in seen:
                seen.add(key)
                targets.append(key)
    for acct in _get_spoke_accounts():
        for rgn in SPOKE_REGIONS:
            key = (acct, rgn)
            if key not in seen:
                seen.add(key)
                targets.append(key)
    return targets


# ============================================================================
# FLEET CACHE INFRASTRUCTURE
# ============================================================================

_fleet_summary_cache: Optional[dict] = None
_fleet_instances_cache: Optional[dict] = None
# True when the cached fleet stopped at MAX_FLEET_SIZE with more instances to read.
_fleet_view_truncated = False
_fleet_view_unreadable: List[str] = []
_fleet_view_error: Optional[str] = None
_fleet_summary_cache_time: float = 0
_FLEET_SUMMARY_CACHE_TTL = 300
_fleet_cache_lock = threading.Lock()


def _direct_describe_scoped_instances(
    instance_ids: Optional[List[str]] = None,
    accounts: Optional[List[str]] = None,
    regions: Optional[List[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Describe scope-tagged EC2 instances across configured (account, region).

    The agent's primary fleet source is SSM Explorer (Resource Data Sync), but
    Explorer has 30 minutes to ~1 day ingestion lag -- up to ~6 hours on first
    sync after deploy. Inspector and EC2 see new instances immediately. This
    creates contradictory agent behaviour: Vulnerability Analyst answers about
    a new instance but Patch Manager / Fleet Overview can't see it.

    This helper is the authoritative direct-EC2 source. It calls
    `EC2:describe_instances` with the SCOPE_TAG filter across configured
    (account, region) pairs, returning per-instance routing metadata
    independent of Explorer's state.

    Used by:
      * `_validate_instance_scope` -- confirms instance exists AND is tagged,
        across spokes (today's hub-only fallback misses spoke instances).
      * `_group_instances_by_location` -- Fix #1 fallback for cache misses.
      * `get_fleet_overview` -- hybrid sourcing so the fleet view always
        reflects reality, with a marker on rows pending Explorer ingestion.

    Args:
        instance_ids: Restrict the describe to these IDs. None = describe every
                      scope-tagged instance in the configured (account, region).
        accounts: Restrict to these accounts. Defaults to configured scope
                  accounts (with hub merged in).
        regions: Restrict to these regions. Defaults to SPOKE_REGIONS.

    Returns:
        {instance_id: {account_id, region, tags, state, name, instance_type}}
        Empty dict if nothing matches.
    """
    try:
        hub_id = get_hub_account_id()
    except Exception:
        hub_id = None

    if accounts is None:
        scope = list(_get_configured_scope_accounts() or [])
        if hub_id and hub_id not in scope:
            scope.insert(0, hub_id)
        accounts = scope or ([hub_id] if hub_id else [])

    target_regions = regions or SPOKE_REGIONS
    if not accounts or not target_regions:
        return {}

    # Request-scoped cache: avoid redundant describe_instances within one tool chain
    cache = _request_instance_cache.get()
    if cache is None:
        cache = {}
        _request_instance_cache.set(cache)

    # If specific instance_ids requested, return cached ones immediately
    # and only describe the rest
    cached_results: Dict[str, Dict[str, Any]] = {}
    if instance_ids:
        cached_results = {iid: cache[iid] for iid in instance_ids if iid in cache}
        instance_ids = [iid for iid in instance_ids if iid not in cache]
        if not instance_ids:
            logger.info(f"[DIRECT_DESCRIBE] All {len(cached_results)} instance(s) served from request cache")
            return cached_results

    base_filters = [{'Name': f'tag:{SCOPE_TAG_KEY}', 'Values': [SCOPE_TAG_VALUE]}]
    if instance_ids:
        # A filter, not InstanceIds: InstanceIds fails the whole call when any ID
        # lives elsewhere, which hid instances when the IDs spanned locations.
        base_filters = base_filters + [{'Name': 'instance-id', 'Values': list(instance_ids)}]

    def describe(account: str, region: str) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        try:
            ec2 = get_client('ec2', account_id=account, region=region)
            for page in ec2.get_paginator('describe_instances').paginate(Filters=base_filters):
                for res in page.get('Reservations', []):
                    for inst in res.get('Instances', []):
                        iid = inst.get('InstanceId')
                        if not iid:
                            continue
                        tags = {t['Key']: t['Value'] for t in inst.get('Tags', [])}
                        out[iid] = {
                            'account_id': account,
                            'region': region,
                            'tags': tags,
                            'state': inst.get('State', {}).get('Name', 'unknown'),
                            'name': tags.get('Name', ''),
                            'instance_type': inst.get('InstanceType', ''),
                            # EC2 sets Platform to "windows" for Windows instances and leaves it out otherwise.
                            'platform': 'Windows' if inst.get('Platform') == 'windows' else 'Linux/UNIX',
                        }
        except ClientError as e:
            code = e.response.get('Error', {}).get('Code', '')
            # Log at ERROR for access/auth failures -- these silently cause
            # false InstanceOutOfScope rejections in _validate_instance_scope.
            unreadable.append(f"{account}/{region} ({code or 'error'})")
            if code in ('AccessDenied', 'UnauthorizedOperation', 'AuthFailure'):
                logger.error(f"[DIRECT_DESCRIBE] AUTH FAILURE {account}/{region}: {code}. "
                             f"Check PatchySpokeRole trust policy. Instances in this account "
                             f"will be rejected as out-of-scope.")
            else:
                logger.warning(f"[DIRECT_DESCRIBE] {account}/{region}: {code} - {e}")
        except Exception as e:
            logger.error(f"[DIRECT_DESCRIBE] {account}/{region} UNEXPECTED: {type(e).__name__}: {e}")
            unreadable.append(f"{account}/{region} ({type(e).__name__})")
        return out

    # Every (account, region) at once; each call is independent and read-only.
    found = _Described()
    unreadable: List[str] = []
    pairs = [(a, r) for a in accounts for r in target_regions]
    with ThreadPoolExecutor(max_workers=min(_FANOUT_WORKERS, len(pairs))) as pool:
        for part in pool.map(lambda p: describe(*p), pairs):
            found.update(part)
    found.unreadable = unreadable

    # Populate request-scoped cache with newly-described instances
    cache.update(found)
    _request_instance_cache.set(cache)

    # Merge cached results back into the return value
    if cached_results:
        found.update(cached_results)

    return found


class _Described(dict):
    """Instances found by a direct EC2 describe, and the locations that couldn't be read (unreadable)."""
    unreadable: List[str] = []


def environment_instances(environment: str, account_ids: Optional[List[str]] = None,
                          regions: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
    """Scoped instances in an environment: {instance_id: {account_id, region, pending_explorer}}.

    The fleet view (SSM Explorer) first, then scope-tagged instances EC2 reports in
    the environment that Explorer hasn't ingested yet. An environment run targets
    the tag, so it reaches those too, and a plan's checks have to see them (2 Nov:
    after the sample fleet was rebuilt, every dev CVE plan said "no package fix",
    because Inspector's findings were on instances Explorer didn't list yet).
    """
    want = _normalize_environment(environment or '')
    _get_fleet_summary()
    # .unreadable names the locations that couldn't be listed, so a caller can tell a short
    # list from a complete one (disruption round, 3 Nov).
    out: Dict[str, Dict[str, Any]] = _Described()
    for iid, info in fleet_instances().items():
        if (info.get('managed') and _normalize_environment(info.get('environment') or '') == want
                and (not account_ids or info.get('account_id') in account_ids)
                and (not regions or info.get('region') in regions)):
            out[iid] = {'account_id': info.get('account_id'), 'region': info.get('region'), 'pending_explorer': False,
                        'environment_tag': info.get('environment')}
    key = ('scoped-instances', tuple(sorted(account_ids or [])), tuple(sorted(regions or [])))
    try:
        direct = request_cached(key, lambda: _direct_describe_scoped_instances(
            accounts=list(account_ids) if account_ids else None, regions=list(regions) if regions else None))
    except Exception as e:
        logger.warning(f"[FLEET_SUMMARY] direct lookup for {environment} failed: {e}")
        direct = {}
    known = fleet_instances()
    for iid, info in direct.items():
        if iid in out or iid in known or info.get('state') not in ('running', 'pending'):
            continue  # an instance Explorer already lists keeps Explorer's view of it
        if (account_ids and info.get('account_id') not in account_ids) or (regions and info.get('region') not in regions):
            continue
        tag = (info.get('tags') or {}).get('Environment') or ''
        if _normalize_environment(tag) == want:
            out[iid] = {'account_id': info['account_id'], 'region': info['region'], 'pending_explorer': True,
                        'environment_tag': tag}
    pending = sum(1 for v in out.values() if v['pending_explorer'])
    if pending:
        logger.info(f"[FLEET_SUMMARY] {environment}: {pending} instance(s) not in Explorer yet, from EC2")
    out.unreadable = location_names(list(fleet_view_unreadable()) + list(getattr(direct, 'unreadable', None) or []))
    return out


def location_names(locations: List[str]) -> List[str]:
    """Unread locations, one entry each: "acct/region (Code)" and "acct/region" are the same place."""
    best: Dict[str, str] = {}
    for loc in locations:
        key = str(loc).split(' (')[0]
        if key not in best or len(str(loc)) > len(best[key]):
            best[key] = str(loc)
    return sorted(best.values())


_fleet_refresh_lock = threading.Lock()


def _get_fleet_summary(environment: Optional[str] = None) -> dict:
    """Fleet summary, cached; when it's stale one caller refreshes it and the others wait for that."""
    with _fleet_cache_lock:
        if _fleet_summary_cache is not None and (time.time() - _fleet_summary_cache_time) < _FLEET_SUMMARY_CACHE_TTL:
            return _filter_fleet_summary(_fleet_summary_cache, environment)
    with _fleet_refresh_lock:
        return _refresh_fleet_summary(environment)


def _refresh_fleet_summary(environment: Optional[str] = None) -> dict:
    """Fleet summary via SSM Explorer GetOpsSummary with Resource Data Sync."""
    global _fleet_summary_cache, _fleet_summary_cache_time, _fleet_instances_cache, _fleet_view_truncated
    global _fleet_view_unreadable, _fleet_view_error
    now = time.time()
    with _fleet_cache_lock:
        if _fleet_summary_cache is not None and (now - _fleet_summary_cache_time) < _FLEET_SUMMARY_CACHE_TTL:
            return _filter_fleet_summary(_fleet_summary_cache, environment)

    try:
        # Step 1: Get all managed instances from Explorer (single API call)
        client = get_client('ssm', region=AWS_REGION)
        instances = {}
        next_token = None

        while len(instances) < MAX_FLEET_SIZE:
            kwargs: Dict[str, Any] = {
                'SyncName': EXPLORER_SYNC_NAME,
                'ResultAttributes': [{'TypeName': 'AWS:EC2InstanceInformation'}],
                'Filters': EXPLORER_INSTANCE_FILTER,
                'MaxResults': 50,
            }
            if next_token:
                kwargs['NextToken'] = next_token

            resp = client.get_ops_summary(**kwargs)

            for entity in resp.get('Entities', []):
                iid = entity.get('Id', '')
                if not iid.startswith('i-'):
                    continue
                content = (entity.get('Data', {}).get('AWS:EC2InstanceInformation', {}).get('Content') or [{}])[0]
                instances[iid] = {
                    'account_id': content.get('SourceAccountId', ''),
                    'region': content.get('SourceRegion', AWS_REGION),
                    'ping_status': 'Online' if content.get('IsManaged') == 'true' else 'Unknown',
                    'platform': content.get('PlatformName', ''),
                    'environment': 'unknown',
                    'managed': False,
                    'missing_count': 0,
                    'installed_count': 0,
                    'failed_count': 0,
                    'scanned': False,  # set when Systems Manager has a patch state for it
                }

            next_token = resp.get('NextToken')
            if not next_token:
                break

        truncated = bool(next_token)
        if truncated:
            logger.warning(f"[FLEET_SUMMARY] Stopped at MAX_FLEET_SIZE={MAX_FLEET_SIZE}; more instances exist")
        logger.info(f"[FLEET_SUMMARY] Explorer returned {len(instances)} instances")

        # -- Scope filter --
        # Explorer's Resource Data Sync ingests OpsData from EntireOrganization,
        # so it returns instances from accounts that aren't part of the operator's
        # configured scope (e.g., management account). Drop anything outside the
        # allowlist before paying the cost of EC2/SSM enrichment.
        allowed = _get_configured_scope_accounts()
        if allowed:
            before = len(instances)
            instances = {iid: inst for iid, inst in instances.items()
                         if inst.get('account_id') in allowed}
            dropped = before - len(instances)
            if dropped:
                logger.info(f"[FLEET_SUMMARY] Scope filter: dropped {dropped} instance(s) "
                            f"from accounts outside configured scope ({len(allowed)} allowed)")

        # Step 2: Enrich with tags and patch data per (account, region) bucket.
        # Bucketing by account alone (the previous shape) caused cross-region instances
        # to inherit the FIRST instance's region for the EC2/SSM lookups, which silently
        # dropped any instance in a different region. Now each (account, region) pair
        # gets its own boto3 client and lookup batch.
        by_account_region: Dict[tuple, list] = {}
        for iid, inst in instances.items():
            key = (inst['account_id'], inst['region'])
            by_account_region.setdefault(key, []).append(iid)

        def enrich(account_id: str, region: str, iids: List[str]):
            """Tags and patch state for one (account, region); returns updates to apply.

            Explorer keeps terminated instances (no state, IsManaged still true), so
            when EC2 answers, an instance it reports terminated or doesn't list is gone.
            """
            tags_by_id: Dict[str, dict] = {}
            patch_by_id: Dict[str, dict] = {}
            gone: List[str] = []
            try:
                ec2 = get_client('ec2', account_id=account_id, region=region)
                paginator = ec2.get_paginator('describe_instances')
                # instance-id filter, not InstanceIds: Explorer still lists terminated
                # instances for a while, and one unknown ID fails an InstanceIds call
                # for the whole bucket.
                for i in range(0, len(iids), _EC2_FILTER_VALUES):
                    chunk = iids[i:i + _EC2_FILTER_VALUES]
                    for page in paginator.paginate(Filters=[{'Name': 'instance-id', 'Values': chunk}]):
                        for res in page.get('Reservations', []):
                            for inst in res.get('Instances', []):
                                if (inst.get('State') or {}).get('Name') in ('shutting-down', 'terminated'):
                                    continue
                                tags_by_id[inst['InstanceId']] = {t['Key']: t['Value'] for t in inst.get('Tags', [])}
                gone = [i for i in iids if i not in tags_by_id]
            except Exception as e:
                logger.warning(f"[FLEET_SUMMARY] Tag lookup failed for {account_id}/{region}: {e}")
                unreadable.append(f"{account_id}/{region}")
            from helper.retry import throttled
            try:
                ssm_client = get_client('ssm', account_id=account_id, region=region)
                for i in range(0, len(iids), 50):
                    resp = throttled(lambda: ssm_client.describe_instance_patch_states(InstanceIds=iids[i:i + 50]))
                    for state in resp.get('InstancePatchStates', []):
                        patch_by_id[state['InstanceId']] = state
            except Exception as e:
                # The instances there have no patch state to show; they read as not scanned,
                # not as up to date (2 Nov review, L5, L8).
                logger.warning(f"[FLEET_SUMMARY] Patch state lookup failed for {account_id}/{region}: {e}")
            return tags_by_id, patch_by_id, gone

        unreadable: List[str] = []
        buckets = list(by_account_region.items())
        if buckets:
            with ThreadPoolExecutor(max_workers=min(_FANOUT_WORKERS, len(buckets))) as pool:
                results = list(pool.map(lambda b: enrich(b[0][0], b[0][1], b[1]), buckets))
            for tags_by_id, patch_by_id, gone in results:
                for iid in gone:
                    instances.pop(iid, None)
                for iid, tags in tags_by_id.items():
                    if iid in instances:
                        instances[iid]['environment'] = tags.get('Environment', 'unknown')
                        instances[iid]['name'] = tags.get('Name', '')
                        instances[iid]['report_tags'] = _pick_report_tags(tags)
                        instances[iid]['managed'] = tags.get(SCOPE_TAG_KEY) == SCOPE_TAG_VALUE
                for iid, state in patch_by_id.items():
                    if iid in instances:
                        instances[iid]['missing_count'] = state.get('MissingCount', 0)
                        instances[iid]['installed_count'] = state.get('InstalledCount', 0)
                        instances[iid]['failed_count'] = state.get('FailedCount', 0)
                        instances[iid]['scanned'] = True

        with _fleet_cache_lock:
            _fleet_summary_cache = _aggregate_fleet(instances)
            _fleet_instances_cache = instances
            _fleet_view_truncated = truncated
            _fleet_view_unreadable = sorted(unreadable)
            _fleet_view_error = None
            _fleet_summary_cache_time = now
        logger.info(f"[FLEET_SUMMARY] Cached: {len(instances)} instances, "
                     f"{len(by_account_region)} (account, region) buckets")
        return _filter_fleet_summary(_fleet_summary_cache, environment)

    except Exception as e:
        error_code = getattr(e, 'response', {}).get('Error', {}).get('Code', '') if hasattr(e, 'response') else type(e).__name__
        logger.error(f"[FLEET_SUMMARY] Explorer unavailable ({error_code})")
        # The previous view stays in the cache but may be hours old; plans need to know (2 Nov review, M3).
        _fleet_view_error = error_code or 'error'
        return {
            'accounts': {}, 'environments': {}, 'totals': {},
            'explorer_available': False,
            'error': f"SSM Explorer sync '{EXPLORER_SYNC_NAME}' not available ({error_code}). "
                     "Run ./deploy.sh to create it, or verify Quick Setup completed.",
        }


def _aggregate_fleet(instances: dict) -> dict:
    """Group per-instance data by account and environment."""
    accounts: Dict[str, Dict[str, Any]] = {}
    environments: Dict[str, Dict[str, Any]] = {}

    for iid, inst in instances.items():
        acct = inst['account_id'] or 'unknown'
        env = inst['environment']
        managed = inst['managed']
        online = inst['ping_status'] == 'Online'

        for group_key, group_map in [('account', accounts), ('environment', environments)]:
            key = acct if group_key == 'account' else env
            if key not in group_map:
                group_map[key] = {
                    'total': 0, 'managed': 0, 'unmanaged': 0,
                    'online': 0, 'offline': 0,
                    'missing_patches': 0, 'installed_patches': 0, 'failed_patches': 0,
                    'compliant_instances': 0, 'scanned_instances': 0,
                }
            g = group_map[key]
            g['total'] += 1
            g['managed' if managed else 'unmanaged'] += 1
            g['online' if online else 'offline'] += 1
            if managed:
                g['missing_patches'] += inst['missing_count']
                g['installed_patches'] += inst['installed_count']
                g['failed_patches'] += inst['failed_count']
                if inst['missing_count'] >= 0 and inst['installed_count'] > 0:
                    g['scanned_instances'] += 1
                    if inst['missing_count'] == 0:
                        g['compliant_instances'] += 1

    return {'accounts': accounts, 'environments': environments, 'explorer_available': True}


def _filter_fleet_summary(summary: dict, environment: Optional[str] = None) -> dict:
    """Return subset of cached summary for a specific environment."""
    if not environment:
        return summary
    env_data = summary.get('environments', {}).get(environment)
    if not env_data:
        return {'accounts': {}, 'environments': {}, 'totals': {}, 'explorer_available': summary.get('explorer_available', False)}
    return {
        'accounts': summary['accounts'],
        'environments': {environment: env_data},
        'explorer_available': summary.get('explorer_available', True),
    }


# ============================================================================
# INSTANCE GROUPING
# ============================================================================


def _group_instances_by_location(instance_ids: List[str]) -> Dict[Tuple[str, str], List[str]]:
    """Group instance IDs by their owning (account_id, region) pair.

    Critical for cross-account/cross-region instance-ID Automation. SSM
    TargetLocations expands a single Automation into one child execution per
    (account, region) -- and EACH child receives the FULL instance-ID list as
    parameter values. Children targeting account A can't see instances owned
    by account B, so those invocations fail silently (InvalidInstanceId).

    Resolution order per instance:
      1. Explorer cache (`_fleet_instances_cache`) -- fast path.
      2. Direct EC2 describe via `_direct_describe_scoped_instances` (shared
         with `_validate_instance_scope` and `get_fleet_overview`). Catches
         instances Explorer hasn't ingested yet (30 min to ~1 day lag).
      3. Fall back to (hub_id, AWS_REGION) and log a warning if even direct
         describe couldn't find the instance.

    Raises:
        RuntimeError if NONE of the named instances can be located in any
        configured (account, region). Every Automation we start would fail --
        better to surface the cause now.

    Returns: {(account_id, region): [instance_ids]}
    """
    _get_fleet_summary()  # ensure fleet cache is warm
    cache = _fleet_instances_cache or {}
    try:
        hub_id = get_hub_account_id()
    except Exception:
        hub_id = None

    # Diagnostic -- confirms the new code path is running. Logs once per call.
    cache_hits = sum(1 for iid in instance_ids if iid in cache)
    cache_misses = len(instance_ids) - cache_hits
    logger.info(
        f"[GROUP_INSTANCES] called with {len(instance_ids)} instance(s): "
        f"{cache_hits} cache hit(s), {cache_misses} miss(es). "
        f"hub=({hub_id}, {AWS_REGION})"
    )

    # Step 2: direct EC2 describe across configured (account, region) for any
    # cache miss. Shared with `_validate_instance_scope` and `get_fleet_overview`
    # via `_direct_describe_scoped_instances`.
    unknowns = [iid for iid in instance_ids if iid not in cache]
    resolved_by_describe: Dict[str, Tuple[str, str]] = {}
    if unknowns:
        described = _direct_describe_scoped_instances(instance_ids=unknowns)
        for iid, info in described.items():
            resolved_by_describe[iid] = (info['account_id'], info['region'])

    groups: Dict[Tuple[str, str], List[str]] = {}
    truly_unresolved: List[str] = []
    for iid in instance_ids:
        if iid in cache:
            cached = cache[iid] or {}
            account = cached.get('account_id') or hub_id or ''
            region = cached.get('region') or AWS_REGION
        elif iid in resolved_by_describe:
            account, region = resolved_by_describe[iid]
            logger.info(f"[GROUP_INSTANCES] {iid} not in Explorer cache; "
                        f"resolved via direct describe to ({account}, {region})")
        else:
            account = hub_id or ''
            region = AWS_REGION
            truly_unresolved.append(iid)
        groups.setdefault((account, region), []).append(iid)

    # Fail fast when nothing resolved -- every Automation we start is doomed.
    if instance_ids and len(truly_unresolved) == len(instance_ids):
        raise RuntimeError(
            f"None of the {len(instance_ids)} instance ID(s) could be located "
            f"in Explorer or via direct EC2 describe across configured accounts "
            f"and regions. Possible causes: invalid instance IDs, spoke role not "
            f"deployed in target accounts, SPOKE_REGIONS misconfigured, or "
            f"instances terminated. Instances: {instance_ids[:5]}"
        )

    if truly_unresolved:
        logger.warning(
            f"[GROUP_INSTANCES] {len(truly_unresolved)}/{len(instance_ids)} "
            f"instance(s) unresolved after Explorer and direct describe; "
            f"defaulting to ({hub_id}, {AWS_REGION}). The Automation child for "
            f"these will likely fail with InvalidInstanceId. "
            f"Unresolved: {truly_unresolved[:5]}"
        )

    return groups


def _locate_instances(instance_ids: List[str]) -> Dict[Tuple[str, str], List[str]]:
    """Like _group_instances_by_location, for read paths: never raises.

    Falls back to the hub when nothing resolves, so callers report the
    instances as not found instead of failing the whole call.
    """
    try:
        return _group_instances_by_location(instance_ids)
    except RuntimeError as e:
        logger.warning(f"[GROUP_INSTANCES] {e}")
        return {(None, AWS_REGION): list(instance_ids)} if instance_ids else {}


# ============================================================================
# SLA HELPERS
# ============================================================================


def _calculate_sla_requirement(frameworks: List[str], severity: str,
                               instance_tags: Optional[Dict[str, str]] = None) -> Optional[dict]:
    """Calculate SLA requirement from EC2 instance tags.

    Reads SLA-CRITICAL, SLA-HIGH, SLA-MEDIUM, SLA-LOW tags from the instance.
    Falls back to global defaults if tags are missing.

    This is fully tag-driven -- the solution has no built-in knowledge of
    compliance frameworks. Customers set SLA hours on their instances via
    tags, driven by their CMDB, tag policies, or manual configuration.
    """
    try:
        sev = severity.upper()
        tag_key = f"SLA-{sev}"

        # Read SLA from instance tag
        if instance_tags:
            tag_val = instance_tags.get(tag_key)
            if tag_val:
                try:
                    sla_hours = int(tag_val)
                    source = f"tag:{tag_key}"
                    logger.info(f"SLA from tag: {tag_key}={sla_hours} frameworks={frameworks}")
                    return {"sla_hours": sla_hours, "frameworks": frameworks,
                            "source": source, "severity": sev}
                except ValueError:
                    logger.warning(f"Invalid SLA tag value: {tag_key}={tag_val}")

        # Fall back to global defaults
        sla_hours = _DEFAULT_SLA.get(sev, 24)
        logger.info(f"SLA from default: {sev}={sla_hours} (no {tag_key} tag found)")
        return {"sla_hours": sla_hours, "frameworks": frameworks,
                "source": "default", "severity": sev}
    except Exception as e:
        logger.error(f"Could not calculate SLA: {e}", exc_info=True)
        return None


def _instance_tags_by_id(instance_ids: List[str]) -> Dict[str, Dict[str, str]]:
    """Each instance's tags, read in its own account and Region.

    An instance-id filter, not InstanceIds: one terminated ID fails an
    InstanceIds call for every instance in that location. Locations come from
    the fleet cache, or a direct describe for instances it doesn't have.
    """
    cache = _fleet_instances_cache or {}
    by_loc: Dict[Tuple[str, str], List[str]] = {}
    unknowns: List[str] = []
    for iid in instance_ids:
        cached = cache.get(iid)
        if cached and cached.get('account_id') and cached.get('region'):
            by_loc.setdefault((cached['account_id'], cached['region']), []).append(iid)
        else:
            unknowns.append(iid)
    if unknowns:
        for iid, info in _direct_describe_scoped_instances(instance_ids=unknowns).items():
            by_loc.setdefault((info['account_id'], info['region']), []).append(iid)
    from helper.retry import throttled
    tags_by_id = _Tags()
    unreadable: List[str] = []
    for (account, region), iids in by_loc.items():
        try:
            ec2 = get_client('ec2', account_id=account, region=region)
            for n in range(0, len(iids), _EC2_FILTER_VALUES):
                pages = throttled(lambda: list(ec2.get_paginator('describe_instances').paginate(
                    Filters=[{'Name': 'instance-id', 'Values': iids[n:n + _EC2_FILTER_VALUES]}])))
                for page in pages:
                    for res in page.get('Reservations', []):
                        for inst in res.get('Instances', []):
                            tags_by_id[inst['InstanceId']] = {t['Key']: t['Value'] for t in inst.get('Tags', [])}
        except Exception as e:
            logger.warning(f"[SLA_RESOLVE] tag lookup failed for {account}/{region}: {e}")
            unreadable.extend(i for i in iids if i not in tags_by_id)
    tags_by_id.unreadable = unreadable
    return tags_by_id


class _Tags(dict):
    """Tags by instance ID, and the instances whose tags couldn't be read (unreadable)."""
    unreadable: List[str] = []


def _sla_hours_each(instance_ids: List[str], severity: Optional[str]) -> Dict[str, int]:
    """SLA hours per instance for a severity, from its SLA tags, or the default."""
    sev = (severity or '').upper()
    if sev not in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        return {}
    tags_by_id = _instance_tags_by_id(instance_ids)
    out: Dict[str, int] = {}
    for iid in instance_ids:
        tags = tags_by_id.get(iid)
        fw = [f.strip() for f in (tags or {}).get('ComplianceFrameworks', '').split(',') if f.strip()]
        sla = _calculate_sla_requirement(fw, sev, instance_tags=tags or {}) if tags is not None else None
        out[iid] = int(sla['sla_hours']) if sla and sla.get('sla_hours') is not None else _DEFAULT_SLA.get(sev, 24)
    return out


SLA_TAGS_UNREADABLE = "tags unreadable"


def _resolve_sla_for_instances(instance_ids: List[str], severity: Optional[str]) -> dict:
    """Resolve SLA hours and source from instance tags, taking the strictest.

    Used by patch tools to populate the compliance context with SLA data even
    when the agent didn't explicitly forward sla_hours/sla_source. Without
    this, Path A reports (operator-named instance, no fleet discovery) end
    up with sla_hours=null in S3 and the SLA Compliance dashboard shows
    "0 met / 0 breached" because every report is "unknown."

    Reads each instance's `SLA-<SEVERITY>` and `ComplianceFrameworks` tags.
    When multiple instances are targeted, returns the lowest sla_hours
    (the binding constraint) and the framework that produced it.

    Falls back to env-derived defaults if tags are missing. Never returns
    None for sla_hours unless severity is unknown -- emergencies still get
    an SLA stamp for the audit trail, just one derived from defaults.

    Args:
        instance_ids: EC2 instance IDs to resolve SLA for. May span accounts/regions.
        severity: CVE severity (CRITICAL/HIGH/MEDIUM/LOW). Lowercased.

    Returns:
        {sla_hours: int|None, sla_source: str|None, frameworks: list[str]}
    """
    if not severity:
        return {"sla_hours": None, "sla_source": None, "frameworks": []}

    sev = severity.upper() if severity else None
    if sev not in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        return {"sla_hours": None, "sla_source": None, "frameworks": []}

    tags_by_id = _instance_tags_by_id(instance_ids)
    unread = list(getattr(tags_by_id, 'unreadable', []) or [])
    if unread:
        # Some instances' SLA tags couldn't be read: their SLA could be stricter than
        # anything read, so the result is not a default and can't support waiting for a
        # window (2 Nov review, M3).
        read = [_calculate_sla_requirement([], sev, instance_tags=t) for t in tags_by_id.values()]
        hours = min([int(r['sla_hours']) for r in read if r and r.get('sla_hours') is not None]
                    or [_DEFAULT_SLA.get(sev, 24)])
        return {"sla_hours": hours, "sla_source": SLA_TAGS_UNREADABLE, "frameworks": [],
                "sla_unreadable": len(unread),
                "sla_explanation": (f"SLA {hours}h from the instances whose tags could be read; "
                                    f"{len(unread)} instance(s)' SLA tags couldn't be read")}
    if not tags_by_id:
        # Couldn't locate any instance -- fall back to env defaults so we
        # still record SOMETHING in the audit trail.
        sla_hours = _DEFAULT_SLA.get(sev, 24)
        return {"sla_hours": sla_hours, "sla_source": "default", "frameworks": [],
                "sla_explanation": f"SLA {sla_hours}h (default; no SLA tag on instances)"}

    strictest_hours: Optional[int] = None
    strictest_source: Optional[str] = None
    all_frameworks: List[str] = []
    for tags in tags_by_id.values():
        fw_str = tags.get('ComplianceFrameworks', '')
        fw_list = [f.strip() for f in fw_str.split(',') if f.strip()]
        all_frameworks.extend(fw_list)
        sla = _calculate_sla_requirement(fw_list, sev, instance_tags=tags)
        if not sla or sla.get('sla_hours') is None:
            continue
        hours = int(sla['sla_hours'])
        if strictest_hours is None or hours < strictest_hours:
            strictest_hours = hours
            strictest_source = sla.get('source') or 'default'

    if strictest_hours is None:
        # Tags lookup failed for every instance -- last-resort default.
        strictest_hours = _DEFAULT_SLA.get(sev, 24)
        strictest_source = "default"

    # Dedupe frameworks while preserving order.
    seen: set = set()
    unique_frameworks = []
    for fw in all_frameworks:
        if fw not in seen:
            seen.add(fw)
            unique_frameworks.append(fw)

    # Build human-readable explanation of why this SLA was chosen
    framework_str = ', '.join(unique_frameworks) if unique_frameworks else strictest_source or 'default'
    if strictest_source != 'default':
        sla_explanation = f"SLA {strictest_hours}h from {strictest_source} ({framework_str})"
    else:
        sla_explanation = f"SLA {strictest_hours}h (default; no SLA tag on instances)"

    return {
        "sla_hours": strictest_hours,
        "sla_source": strictest_source,
        "frameworks": unique_frameworks,
        "sla_explanation": sla_explanation,
    }


def _window_check(environment: str, instance_ids: List[str], sla_hours: int,
                  first_observed_at: Optional[str] = None) -> Dict[str, Any]:
    """The earliest maintenance window that patches these instances before the SLA
    deadline, and why each other window won't.

    Returns {"window": <best or None>, "skipped": [{"name", "reason"}]}, so a plan
    that runs now can say which windows it passed over.

    The deadline runs from the vulnerability's first observation when known,
    otherwise from now. A window qualifies only if it is enabled, has an
    AWS-RunPatchBaseline task, covers every requested instance, and finishes
    before the deadline.
    """
    # Import here to avoid circular import -- get_maintenance_windows is a @tool
    # function that lives in a domain module.
    from helper.tools import get_maintenance_windows
    skipped: List[Dict[str, str]] = []
    try:
        windows_result = get_maintenance_windows(environment=environment,
                                                  instance_ids=instance_ids)
        windows = windows_result.get('windows') or []
        if not windows:
            return {'window': None, 'skipped': skipped}

        now = datetime.now(timezone.utc)
        clock_start = _coerce_utc(first_observed_at) or now
        deadline = clock_start + timedelta(hours=sla_hours)
        best: Optional[Dict[str, Any]] = None
        best_hours: Optional[float] = None

        for w in windows:
            name = w.get('name', 'unknown')
            if not w.get('enabled', True):
                skipped.append({'name': name, 'reason': 'is disabled'})
                continue
            if not w.get('has_patch_task'):
                skipped.append({'name': name, 'reason': 'has no patch install task'})
                continue
            if instance_ids and w.get('instances_not_covered'):
                skipped.append({'name': name, 'reason': "doesn't target every instance"})
                continue
            win_dt = _coerce_utc(w.get('next_execution_utc'))
            if win_dt is None or win_dt < now:
                skipped.append({'name': name, 'reason': 'has no upcoming run'})
                continue
            win_end = win_dt + timedelta(hours=float(w.get('duration_hours') or 0))
            if win_end > deadline:
                skipped.append({'name': name, 'reason': 'finishes after the SLA deadline '
                                f'({deadline.strftime("%d %b %H:%M UTC")})'})
                continue
            hours = (win_dt - now).total_seconds() / 3600
            if best_hours is None or hours < best_hours:
                best = {
                    'name': name,
                    'window_id': w.get('window_id'),
                    'account_id': w.get('account_id'),
                    'region': w.get('region'),
                    'next_execution': win_dt.isoformat(),
                    'hours_until_window': round(hours, 2),
                    'sla_deadline_utc': deadline.isoformat(),
                }
                best_hours = hours

        return {'window': best, 'skipped': skipped}
    except Exception as e:
        logger.warning(f"[SLA_WINDOW] earliest-window lookup failed: {e}")
        return {'window': None, 'skipped': skipped}


def _earliest_window_within_sla(environment: str, instance_ids: List[str],
                                sla_hours: int, first_observed_at: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The earliest window that patches these instances inside the SLA, or None (see _window_check)."""
    return _window_check(environment, instance_ids, sla_hours, first_observed_at=first_observed_at)['window']


def _window_check_text(sla_hours, sla_source, skipped: List[Dict[str, str]]) -> str:
    """Why a plan runs now rather than in a window, naming the windows that won't do (at most five)."""
    base = f"No maintenance window meets the {sla_hours} h SLA ({sla_source or 'default'})"
    if not skipped:
        return base + ", so it runs now."
    # The same window name can exist in several accounts and Regions; say it once.
    counts: Dict[tuple, int] = {}
    for s in skipped:
        counts[(s['name'], s['reason'])] = counts.get((s['name'], s['reason']), 0) + 1
    # Several unreadable policies read as one clause, not one each (2 Nov screenshots).
    unreadable = [name for (name, reason) in counts if reason.startswith("couldn't be read")]
    items = [f"{name} ({n} locations) {reason}" if n > 1 else f"{name} {reason}"
             for (name, reason), n in counts.items() if not reason.startswith("couldn't be read")]
    if len(unreadable) == 1:
        items.append(f"Quick Setup policy {unreadable[0]} couldn't be read, so it doesn't count")
    elif unreadable:
        items.append(f"{len(unreadable)} Quick Setup policies couldn't be read ({', '.join(unreadable)}), "
                     "so they don't count")
    named = "; ".join(items[:5])
    more = f"; and {len(items) - 5} more" if len(items) > 5 else ""
    return f"{base}: {named}{more}. It runs now."


def _coerce_utc(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 string to an aware UTC datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ============================================================================
# BASELINE OVERRIDE HELPERS
# ============================================================================


def _get_baseline_override_url(severity_filter: Optional[str]) -> Optional[str]:
    """Get the S3 URL for a severity-scoped baseline override.

    Returns None if no severity filter is specified (use default baseline).
    Returns the S3 URL if a valid severity filter is provided and the file exists.
    Logs a warning and returns None if the override file is missing from S3.
    """
    if not severity_filter:
        return None
    key = _SEVERITY_OVERRIDE_MAP.get(severity_filter.upper())
    if not key:
        logger.warning(f"Unknown severity filter: {severity_filter}, using default baseline")
        return None
    bucket = _get_compliance_bucket_name()

    # Verify the override file exists -- SSM fails silently if it doesn't
    try:
        _s3().head_object(Bucket=bucket, Key=key)
    except Exception as e:
        logger.error(f"BaselineOverride file not found: s3://{bucket}/{key} — {e}. "
                     "Run setup_baseline_overrides.py to upload override files.")
        return None

    return f"s3://{bucket}/{key}"


# ============================================================================
# S3 COMPLIANCE BUCKET HELPERS
# ============================================================================


def _get_compliance_bucket_name() -> str:
    """Get S3 bucket name for compliance reports (cached)."""
    if not hasattr(_get_compliance_bucket_name, '_cached'):
        sts_client = boto3.client('sts', region_name=AWS_REGION, config=aws_config)
        account_id = sts_client.get_caller_identity()['Account']
        _get_compliance_bucket_name._cached = f'patch-compliance-reports-{account_id}'
    return _get_compliance_bucket_name._cached


def _get_date_prefix(date: datetime) -> str:
    """Get S3 prefix for a given date."""
    return f"{date.year}/{date.month:02d}/{date.day:02d}/"


class InspectorUnreadable(Exception):
    """Inspector couldn't be read for a CVE in a Region, even after retrying."""


def _active_cve_findings(cve_ids: List[str], account_id: Optional[str] = None,
                         region: Optional[str] = None, instance_ids: Optional[List[str]] = None,
                         strict: bool = False):
    """ACTIVE Inspector findings for these CVEs, in every configured Region and page.

    Inspector is regional, and the hub is the delegated administrator, so each
    Region returns the organisation's findings: only accounts in the configured
    scope are kept, and with instance_ids only findings on those instances.
    Throttling is retried. A read that still fails is logged and skipped, or with
    strict raises InspectorUnreadable, so a caller deciding something can tell
    "couldn't read" from "nothing found" (2 Nov review, H1).
    """
    from helper.retry import throttled
    cves = [c for c in (cve_ids or []) if c and c != 'N/A']
    allowed = _get_configured_scope_accounts() if cves else set()
    wanted = set(instance_ids or [])
    for rgn in ([region] if region else list(SPOKE_REGIONS)) if cves else []:
        client = _inspector(account_id=account_id, region=rgn)
        for cve_id in cves:
            token = None
            while True:
                kwargs: Dict[str, Any] = {
                    'filterCriteria': {
                        'vulnerabilityId': [{'comparison': 'EQUALS', 'value': cve_id}],
                        'findingStatus': [{'comparison': 'EQUALS', 'value': 'ACTIVE'}],
                    },
                    'maxResults': 100,
                }
                if token:
                    kwargs['nextToken'] = token
                try:
                    resp = throttled(lambda: client.list_findings(**kwargs))
                except Exception as e:
                    logger.warning(f"[SLA] Inspector lookup failed for {cve_id} in {rgn}: {e}")
                    if strict:
                        raise InspectorUnreadable(f"Inspector couldn't be read for {cve_id} in {rgn}: {e}") from e
                    break
                for finding in resp.get('findings', []):
                    if allowed and finding.get('awsAccountId') and finding['awsAccountId'] not in allowed:
                        continue
                    if wanted and not wanted & {r.get('id') for r in finding.get('resources') or []}:
                        continue
                    yield finding
                token = resp.get('nextToken')
                if not token:
                    break


def _earliest_first_observed_at(cve_ids: List[str],
                                account_id: Optional[str] = None,
                                region: Optional[str] = None,
                                instance_ids: Optional[List[str]] = None,
                                strict: bool = False) -> Optional[str]:
    """Earliest firstObservedAt across ACTIVE Inspector findings for these CVEs.

    Returns an ISO-8601 UTC string, or None when no CVE is supplied or nothing
    is found. This is the vulnerability's discovery time — the start of the SLA
    clock. With instance_ids, only sightings on those instances count: a plan's
    clock runs from when its own targets were found exposed, not from a sighting
    elsewhere in the organisation (2 Nov review, L2). The earliest of those is the
    worst case. A lookup failure gives None (SLA "unknown"), or with strict raises
    InspectorUnreadable.
    """
    earliest: Optional[datetime] = None
    for finding in _active_cve_findings(cve_ids, account_id, region, instance_ids=instance_ids, strict=strict):
        observed = finding.get('firstObservedAt')
        if observed is None:
            continue
        if not isinstance(observed, datetime):
            try:
                observed = datetime.fromisoformat(str(observed).replace('Z', '+00:00'))
            except Exception:
                continue
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        if earliest is None or observed < earliest:
            earliest = observed
    return earliest.astimezone(timezone.utc).isoformat() if earliest else None


_SEVERITY_RANK = {'CRITICAL': 4, 'HIGH': 3, 'MEDIUM': 2, 'LOW': 1}


def _cve_severity(cve_ids: List[str]) -> Optional[str]:
    """The highest severity Inspector gives these CVEs' ACTIVE findings, or None if it lists none.

    A CVE plan takes its SLA from this rather than from the model, so leaving the
    severity out (or naming a lower one) can't skip or relax the window check.
    Raises InspectorUnreadable when Inspector can't be read: the model's value
    would then decide the SLA (2 Nov review, H1).
    """
    best = None
    for finding in _active_cve_findings(cve_ids, strict=True):
        sev = str(finding.get('severity') or '').upper()
        if _SEVERITY_RANK.get(sev, 0) > _SEVERITY_RANK.get(best or '', 0):
            best = sev
    return best


def _write_pending_compliance_context(execution_id: str, context: Dict[str, Any]) -> None:
    """Write patching context to S3 immediately after starting an automation execution.

    The context is consumed later by the UI's reconciliation flow to generate the
    final compliance report once the automation completes. This decouples report
    generation from the chat session -- reports get generated regardless of whether
    the operator returns to ask for status.

    Stored at: s3://patch-compliance-reports-<account>/pending-reports/<execution_id>.json

    The context contains business-level data the agent has at execution time but
    that's not retrievable from the SSM Automation API (CVE, severity, SLA, operator,
    pre-patch state, etc.). The reconciliation flow merges this with the execution
    outcome to produce the final compliance report.
    """
    try:
        bucket = _get_compliance_bucket_name()
        key = f'pending-reports/{execution_id}.json'
        pc = _plan_controls.get()
        body = {
            'execution_id': execution_id,
            'plan_id': (pc or {}).get('plan_id'),
            'started_at': datetime.now(timezone.utc).isoformat(),
            'operator': get_operator(),
            **context,  # cve_id, environment, severity, sla, instance_ids, etc.
        }
        try:
            body['report_tags'], body['report_tag_values'] = _report_tags(context)
        except Exception as e:  # the report still gets written, with no tags
            logger.warning(f"[PENDING_CONTEXT] Report tags for {execution_id} couldn't be read: {e}")
        # Created once (IAM refuses a write without If-None-Match); the first context stands.
        try:
            _s3().put_object(
                Bucket=bucket,
                Key=key,
                Body=json.dumps(body, default=str).encode('utf-8'),
                ContentType='application/json',
                IfNoneMatch='*',
            )
        except ClientError as e:
            if e.response.get('Error', {}).get('Code') not in ('PreconditionFailed', 'ConditionalRequestConflict'):
                raise
            logger.info(f"[PENDING_CONTEXT] Pending context for {execution_id} already written; kept the first")
            return
        logger.info(f"[PENDING_CONTEXT] Wrote pending context for {execution_id} to s3://{bucket}/{key}")
    except Exception as e:
        # The run has started, so it isn't undone; but without this record no
        # compliance report is written for it, so the run card says so.
        logger.warning(f"[PENDING_CONTEXT] Failed to write pending context for {execution_id}: {e}")
        pc = _plan_controls.get()
        if pc is not None:
            pc.setdefault('unreported', []).append(execution_id)


# ============================================================================
# PATCH STATE HELPERS
# ============================================================================


def _unwrap_patch_state(state: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Unwrap capture_patch_state output if needed.

    capture_patch_state returns {'snapshot': {iid: {...}}, 'timestamp': ..., ...}.
    Downstream consumers (verify_rollback, pending compliance context) expect
    the flat {iid: {...}} format.

    The LLM agent may pass either format. This handles both:
    - If 'snapshot' key exists and its value is a dict -> return the snapshot
    - Otherwise assume it's already the flat {iid: {...}} format -> return as-is
    """
    if state is None:
        return None
    if 'snapshot' in state and isinstance(state['snapshot'], dict):
        return state['snapshot']
    return state


# ============================================================================
# AUTOMATION STARTERS
# ============================================================================


# ── Controls of the approved plan being run ──────────────────────────
# An approved plan runs with the limits and abort alarms it stored (and its
# hash covers), not with whatever the environment says when it starts. Each
# start is idempotent per plan and location, and recorded as soon as it returns.

_plan_controls: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar('_plan_controls', default=None)
_CLIENT_TOKEN_NS = uuid.UUID('6f1c9d2e-5b7a-4c3e-9a41-2f8e0d6b7c15')


@contextlib.contextmanager
def plan_controls(plan_id: str, rollout: Optional[dict] = None, alarms: Optional[List[str]] = None,
                  on_started: Optional[Callable[[str], None]] = None):
    """Run the starts inside this block under an approved plan's stored controls."""
    state = {'plan_id': plan_id, 'rollout': rollout, 'alarms': alarms, 'on_started': on_started}
    token = _plan_controls.set(state)
    try:
        yield state
    finally:
        _plan_controls.reset(token)


def run_limits(environment: Optional[str]) -> Dict[str, str]:
    """Rollout limits for a change: the approved plan's stored ones, else the environment's policy."""
    pc = _plan_controls.get()
    if pc and pc.get('rollout'):
        return dict(pc['rollout'])
    return execution_limits(environment)


def _client_token(key: str) -> Dict[str, str]:
    """ClientToken for a start under an approved plan: the same plan and location always get the same one."""
    pc = _plan_controls.get()
    if not pc or not pc.get('plan_id'):
        return {}
    return {'ClientToken': str(uuid.uuid5(_CLIENT_TOKEN_NS, f"{pc['plan_id']}:{key}"))}


def _started(execution_id: str) -> None:
    """Record a run the moment it starts, so a later failure can't leave it untracked."""
    pc = _plan_controls.get()
    if pc and pc.get('on_started'):
        try:
            pc['on_started'](execution_id)
        except Exception as e:
            logger.warning(f"[RUN] could not record {execution_id} at start: {e}")


def _with_location_alarms(target_locations: List[Dict[str, Any]], operation: str) -> List[Dict[str, Any]]:
    """Add the abort alarms to every target location of an install or rollback (not scans).

    Each location's child execution polls the alarm in its own account and Region, so a
    spoke alarm stops the spoke's run. A parent-level AlarmConfiguration would only be
    read in the hub's Region. Under an approved plan the plan's stored alarm list applies.
    """
    from helper.change_controls import alarm_configuration
    if operation == 'Scan':
        return target_locations
    pc = _plan_controls.get()
    if pc and pc.get('alarms') is not None:
        config = ({'Alarms': [{'Name': a} for a in pc['alarms']], 'IgnorePollAlarmFailure': False}
                  if pc['alarms'] else None)
    else:
        config = alarm_configuration()
    if not config:
        return target_locations
    return [{**loc, 'TargetLocationAlarmConfiguration': config} for loc in target_locations]


def _checked_reboot_option(option: str) -> str:
    if option not in REBOOT_OPTIONS:
        raise ValueError(f"unknown reboot option {option!r}")
    return option


def _apply_install_scope(params: Dict[str, Any], severity_filter: Optional[str],
                         install_override_url: Optional[str]) -> None:
    """Scope an Install to a CVE's packages (InstallOverrideList) or to a severity (BaselineOverride).

    A CVE scope names exactly what installs, so the severity override is left off.
    """
    if install_override_url:
        params['InstallOverrideList'] = [install_override_url]
        return
    override_url = _get_baseline_override_url(severity_filter)
    if override_url:
        params['BaselineOverride'] = [override_url]
    elif severity_filter:
        # The plan was checked against this override moments ago; starting without it would
        # install every approved patch, not the approved severity (2 Nov review, H6).
        raise RuntimeError(f"the {severity_filter} baseline override couldn't be confirmed, so the run wasn't started")


def _start_patch_automation(operation: str, environment: str,
                            account_ids: List[str], regions: List[str],
                            max_concurrency: str, max_errors: str,
                            severity_filter: Optional[str] = None,
                            install_override_url: Optional[str] = None,
                            reboot_option: str = 'NoReboot') -> str:
    """Start tag-based SSM Automation with TargetLocations for cross-account patching.

    Returns the parent AutomationExecutionId (string).

    `max_concurrency` and `max_errors` are the ACCOUNT-LEVEL
    (TargetLocations) values -- how many (account, region) child executions
    run in parallel.

    NOTE: We do NOT set top-level MaxConcurrency/MaxErrors here. The tag-based
    Automation doc has no TargetParameterName, so SSM rejects parent-level rate
    control with InvalidAutomationExecutionParametersException. Instance-level
    concurrency is governed by the doc's aws:runCommand step, which calls
    SendCommand with its own MaxConcurrency/MaxErrors against the resolved
    tag targets. The instance-ID docs (RunPatchBaselineById / RunRollbackById)
    DO use top-level MaxConcurrency since they iterate over an instance list.
    """
    # Layer 3: inner SendCommand fan-out for the tag-based doc. Installs use
    # the environment's limits; scans keep the defaults.
    limits = run_limits(environment) if operation != 'Scan' else execution_limits(None)
    params = {
        'Operation': [operation],
        'Environment': [environment],
        'ScopeTagKey': [SCOPE_TAG_KEY],
        'ScopeTagValue': [SCOPE_TAG_VALUE],
        'RebootOption': [_checked_reboot_option(reboot_option)],
        'MaxConcurrency': [limits['send_command_max_concurrency']],
        'MaxErrors':      [limits['send_command_max_errors']],
    }
    _apply_install_scope(params, severity_filter, install_override_url)

    target_locations = [{
        'Accounts': account_ids,
        'Regions': regions,
        'ExecutionRoleName': SPOKE_EXECUTION_ROLE,
        'TargetLocationMaxConcurrency': max_concurrency,
        'TargetLocationMaxErrors': max_errors,
    }]

    resp = _ssm().start_automation_execution(
        DocumentName=AUTOMATION_DOC_NAME,
        Parameters=params,
        TargetLocations=_with_location_alarms(target_locations, operation),
        **_client_token(f"{operation}:tags:{','.join(account_ids)}:{','.join(regions)}"),
    )
    _started(resp['AutomationExecutionId'])
    return resp['AutomationExecutionId']


def _start_instance_patch_automation(operation: str, instance_ids: List[str],
                                     account_ids: List[str], regions: List[str],
                                     max_concurrency: str, max_errors: str,
                                     severity_filter: Optional[str] = None,
                                     environment: Optional[str] = None,
                                     install_override_url: Optional[str] = None,
                                     reboot_option: str = 'NoReboot') -> str:
    """Start SSM Automation with TargetLocations for instance-ID based patching.

    Uses Patchy-RunPatchBaselineById which targets specific instances by ID
    (--target-parameter-name InstanceId). This gives instance-level precision
    through MAMR without patching all tag-matching instances.

    Returns the parent AutomationExecutionId.
    """
    params: Dict[str, Any] = {
        'Operation': [operation],
        'RebootOption': [_checked_reboot_option(reboot_option)],
        # Layer 3: inner SendCommand. Each Automation iteration targets one
        # instance, so the inner step value is fixed at '1'. Real fan-out
        # comes from Layer 2 (top-level MaxConcurrency below).
        'MaxConcurrency': ['1'],
        'MaxErrors':      ['100%'],
    }
    _apply_install_scope(params, severity_filter, install_override_url)

    target_locations = [{
        'Accounts': account_ids,
        'Regions': regions,
        'ExecutionRoleName': SPOKE_EXECUTION_ROLE,
        'TargetLocationMaxConcurrency': max_concurrency,
        'TargetLocationMaxErrors': max_errors,
    }]

    layer2 = run_limits(environment) if operation != 'Scan' else execution_limits(environment)
    resp = _ssm().start_automation_execution(
        DocumentName=AUTOMATION_BY_ID_DOC_NAME,
        Parameters=params,
        TargetParameterName='InstanceId',
        Targets=[{'Key': 'ParameterValues', 'Values': instance_ids}],
        TargetLocations=_with_location_alarms(target_locations, operation),
        # Layer 2: how many instance Automation iterations run in parallel
        # inside each (account, region) child execution, and how many may
        # fail before SSM stops. Environment limits apply when given.
        MaxConcurrency=layer2['instance_max_concurrency'],
        MaxErrors=layer2['instance_max_errors'],
        **_client_token(f"{operation}:ids:{','.join(account_ids)}:{','.join(regions)}"),
    )
    _started(resp['AutomationExecutionId'])
    return resp['AutomationExecutionId']


def _rollback_window_params(since, until) -> Dict[str, List[str]]:
    """Since and Until for Patchy-YumUndo, checked: whole seconds, since before until."""
    if not (isinstance(since, int) and isinstance(until, int)) or isinstance(since, bool) \
            or since <= 0 or until <= since:
        raise ValueError(f"rollback needs a patch run window, got since={since!r} until={until!r}")
    return {'Since': [str(since)], 'Until': [str(until)]}


def _start_instance_rollback_automation(instance_ids: List[str],
                                        account_ids: List[str], regions: List[str],
                                        max_concurrency: str, max_errors: str,
                                        environment: Optional[str] = None,
                                        since: Optional[int] = None,
                                        until: Optional[int] = None) -> str:
    """Start SSM Automation with TargetLocations for instance-ID based rollback.

    Uses Patchy-RunRollbackById which targets specific instances by ID, and
    undoes the yum transactions that began between since and until.
    Returns the parent AutomationExecutionId.
    """
    window = _rollback_window_params(since, until)
    target_locations = [{
        'Accounts': account_ids,
        'Regions': regions,
        'ExecutionRoleName': SPOKE_EXECUTION_ROLE,
        'TargetLocationMaxConcurrency': max_concurrency,
        'TargetLocationMaxErrors': max_errors,
    }]

    resp = _ssm().start_automation_execution(
        DocumentName=ROLLBACK_BY_ID_DOC_NAME,
        Parameters={
            # Layer 3: inner SendCommand. Each iteration targets one instance.
            'MaxConcurrency': ['1'],
            'MaxErrors':      ['100%'],
            **window,
        },
        TargetParameterName='InstanceId',
        Targets=[{'Key': 'ParameterValues', 'Values': instance_ids}],
        TargetLocations=_with_location_alarms(target_locations, 'Rollback'),
        # Layer 2: instance Automation iterations in parallel per child.
        MaxConcurrency=run_limits(environment)['instance_max_concurrency'],
        MaxErrors=run_limits(environment)['instance_max_errors'],
        **_client_token(f"Rollback:ids:{','.join(account_ids)}:{','.join(regions)}"),
    )
    _started(resp['AutomationExecutionId'])
    return resp['AutomationExecutionId']


# ============================================================================
# ERROR CLASSIFICATION (re-exported)
# ============================================================================

# classify_error is imported from helper.error_handling at the top of this file
# and re-exported here so tool modules can import it from _shared.


# ============================================================================
# __all__ EXPORT LIST
# ============================================================================

__all__ = [
    'fleet_instances',
    'reboot_option',
    # Imports (re-exported for convenience)
    'boto3', 'Config', 'ClientError', 'contextvars', 'json', 'logging',
    'os', 're', 'threading', 'time',
    'datetime', 'timedelta', 'timezone',
    'Any', 'Dict', 'List', 'Optional', 'Tuple',

    # Re-exports from cross_account
    'is_multi_account', 'get_client', 'get_hub_account_id',
    'resolve_scope',
    'EXECUTION_DEFAULTS', 'SPOKE_EXECUTION_ROLE', 'SPOKE_REGIONS', 'execution_limits',

    # Error handling
    'classify_error',

    # Logger
    'logger',

    # Constants
    'AWS_REGION',
    'INSTANCE_ID_PATTERN',
    'SCOPE_TAG_KEY', 'SCOPE_TAG_VALUE',
    'AUTOMATION_DOC_NAME', 'AUTOMATION_BY_ID_DOC_NAME',
    'ROLLBACK_DOC_NAME', 'ROLLBACK_BY_ID_DOC_NAME',
    'EXPLORER_SYNC_NAME', 'MAX_FLEET_SIZE',
    'SPOKE_ACCOUNT_IDS_ENV', 'SPOKE_OU_IDS_ENV',
    '_DEFAULT_SLA',
    '_SEVERITY_OVERRIDE_MAP',
    'aws_config',

    # Context variables
    '_current_operator', '_current_timezone',
    '_current_request_scans', '_request_instance_cache',

    # Operator/timezone context
    'set_operator', 'get_operator', 'set_operator_message', 'get_operator_message',
    'set_session_id', 'get_session_id',
    'set_timezone', 'get_timezone',
    'clear_request_scans', 'request_cached', 'forget_request_cache', 'environment_instances',

    # Time formatting
    '_format_utc_as_local',

    # AWS client factories
    '_ssm', '_ec2', '_s3', '_inspector', '_cloudwatch',

    # Validation helpers
    '_validate_instance_ids',
    '_validate_instance_scope',
    '_instance_environments',
    '_normalize_environment',

    # Cross-account query targets
    '_configured_scope_cache',
    '_resolve_ou_member_accounts',
    '_get_configured_scope_accounts',
    '_validate_account_scope',
    '_validate_regions',
    '_get_spoke_accounts',
    '_get_query_targets',

    # Fleet cache infrastructure
    '_fleet_summary_cache', '_fleet_instances_cache',
    '_fleet_summary_cache_time', '_FLEET_SUMMARY_CACHE_TTL',
    '_fleet_cache_lock',
    '_direct_describe_scoped_instances',
    '_get_fleet_summary',
    '_aggregate_fleet',
    '_filter_fleet_summary',

    # Instance grouping
    '_group_instances_by_location',

    # SLA helpers
    '_calculate_sla_requirement',
    '_resolve_sla_for_instances',
    '_earliest_window_within_sla',
    '_window_check',
    '_sla_hours_each',
    '_instance_tags_by_id',
    '_cve_severity', 'InspectorUnreadable', 'SLA_TAGS_UNREADABLE',
    '_window_check_text',

    # Baseline override helpers
    '_get_baseline_override_url',
    '_apply_install_scope', '_checked_reboot_option', '_rollback_window_params', '_with_location_alarms', 'plan_controls', 'run_limits', '_client_token', '_started',

    # S3 compliance bucket helpers
    '_get_compliance_bucket_name',
    '_get_date_prefix',
    '_earliest_first_observed_at',
    '_write_pending_compliance_context',

    # Patch state helpers
    '_unwrap_patch_state',

    # Automation starters
    '_start_patch_automation',
    '_start_instance_patch_automation',
    '_start_instance_rollback_automation',
]
