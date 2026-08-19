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
import contextvars
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from helper.error_handling import classify_error
from helper.cross_account import (
    is_multi_account, get_client, get_hub_account_id,
    resolve_scope, fan_out, build_target_locations, format_execution_plan,
    EXECUTION_DEFAULTS, SPOKE_EXECUTION_ROLE, SPOKE_REGIONS,
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

# Automation document names deployed to spoke accounts via StackSet.
AUTOMATION_DOC_NAME = os.environ.get('PATCHY_AUTOMATION_DOC', 'Patchy-RunPatchBaseline')
AUTOMATION_BY_ID_DOC_NAME = os.environ.get('PATCHY_AUTOMATION_BY_ID_DOC', 'Patchy-RunPatchBaselineById')
ROLLBACK_DOC_NAME = os.environ.get('PATCHY_ROLLBACK_DOC', 'Patchy-RunRollback')
ROLLBACK_BY_ID_DOC_NAME = os.environ.get('PATCHY_ROLLBACK_BY_ID_DOC', 'Patchy-RunRollbackById')

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
# Scans created this request -- blocks execute in same invocation as dry-run.
_current_request_scans: contextvars.ContextVar[set | None] = contextvars.ContextVar('_current_request_scans', default=None)
# Per-request instance cache -- avoids redundant EC2 describe_instances calls within a single agent invocation.
_request_instance_cache: contextvars.ContextVar[Optional[Dict[str, dict]]] = contextvars.ContextVar('_request_instance_cache', default=None)

# ============================================================================
# OPERATOR / TIMEZONE CONTEXT
# ============================================================================


def set_operator(identity: str) -> None:
    """Set the operator identity for the current request context."""
    _current_operator.set(identity)


def get_operator() -> str:
    """Get the operator identity for the current request context."""
    return _current_operator.get()


def set_timezone(tz_name: str) -> None:
    """Set the user's timezone for the current request context."""
    _current_timezone.set(tz_name or 'UTC')


def get_timezone() -> str:
    """Get the user's timezone for the current request context."""
    return _current_timezone.get()


def clear_request_scans() -> None:
    """Reset scan tracking at the start of each request."""
    _current_request_scans.set(set())


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


def _validate_instance_scope(instance_ids: List[str]) -> Optional[dict]:
    """Returns error dict if any instance is missing the scope tag, None if all in scope.

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
        if _fleet_instances_cache:
            in_scope = {iid for iid in instance_ids
                       if iid in _fleet_instances_cache and _fleet_instances_cache[iid].get('managed')}
        else:
            in_scope = set()

        # Fallback: direct EC2 describe across the full configured scope
        # (hub + spokes x SPOKE_REGIONS) for any instance not in the Explorer
        # cache. The describe applies the SCOPE_TAG filter at the API level
        # so untagged instances never enter `in_scope`.
        unchecked = [iid for iid in instance_ids if iid not in in_scope]
        if unchecked:
            described = _direct_describe_scoped_instances(instance_ids=unchecked)
            for iid in described:
                in_scope.add(iid)

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
                               f"cache but not found by direct describe — retrying: {known_but_missing[:3]}")
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

        out_of_scope = [iid for iid in instance_ids if iid not in in_scope]
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
        logger.warning(f"Scope validation failed: {e}")
    return None


def _validate_account_scope(account_ids: List[str]) -> Optional[dict]:
    """Returns an error dict if any account is outside the configured scope, None otherwise.

    The tag-based write paths (multi_account_execute / multi_account_rollback)
    take account_ids directly rather than deriving them from scope-validated
    instances, so they need their own account check — the read paths already
    filter to _get_configured_scope_accounts(). An empty configured scope is
    the "no filter" sentinel (both SPOKE_OU_IDS and SPOKE_ACCOUNT_IDS unset)
    and permits everything, preserving single-account / unconfigured behaviour.
    """
    allowed = _get_configured_scope_accounts()
    if not allowed:
        return None  # no allowlist configured — nothing to enforce
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
            "next_action": "The operation was NOT started. Present this to the operator. Do NOT use get_response_template('operation_initiated').",
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


def _resolve_ou_member_accounts(ou_ids: List[str]) -> List[str]:
    """Resolve OU IDs to active member account IDs via Organizations.

    Returns [] (and logs) on failure so the caller can degrade gracefully. The
    hub account is included if it's in one of the listed OUs.
    """
    if not ou_ids:
        return []
    try:
        from helper.cross_account import discover_accounts, get_hub_account_id as _hub
        # discover_accounts excludes the hub by default -- we want the hub back IF it's
        # genuinely in one of the listed OUs. Caller will union with the hub anyway,
        # so excluding it here is harmless.
        members = discover_accounts(ou_ids=ou_ids)
        return [m['account_id'] for m in members]
    except Exception as e:
        logger.warning(f"[CROSS_REGION] Could not resolve OU members for {ou_ids}: {e}")
        return []


def _get_configured_scope_accounts() -> set:
    """Return the operator's configured scope (account allowlist) for filtering.

    Precedence (Option C -- OUs win):
      1. SPOKE_OU_IDS set -> resolve OU members + hub.
      2. SPOKE_OU_IDS unset, SPOKE_ACCOUNT_IDS set -> use that list + hub.
      3. Both unset -> empty set (sentinel meaning "no filter").

    The empty-set sentinel preserves backward compatibility: code that calls
    this function should treat an empty result as "don't filter, include
    everything Inspector/Explorer returns."

    Cached for process lifetime to avoid hammering Organizations on every call.
    """
    global _configured_scope_cache
    if _configured_scope_cache is not None:
        return _configured_scope_cache

    if not is_multi_account():
        # Single-account mode -- scope is just the hub. Return as a set so the
        # filter logic still applies (drops anything Inspector returns from
        # other accounts, even though that shouldn't happen here).
        try:
            _configured_scope_cache = {get_hub_account_id()}
        except Exception:
            _configured_scope_cache = set()
        return _configured_scope_cache

    try:
        hub_id = get_hub_account_id()
    except Exception:
        hub_id = None

    if SPOKE_OU_IDS_ENV:
        members = _resolve_ou_member_accounts(SPOKE_OU_IDS_ENV)
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
    return scope


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
        members = _resolve_ou_member_accounts(SPOKE_OU_IDS_ENV)
        return list(dict.fromkeys(a for a in members if a and a != hub_id))
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

    found: Dict[str, Dict[str, Any]] = {}
    base_filters = [{'Name': f'tag:{SCOPE_TAG_KEY}', 'Values': [SCOPE_TAG_VALUE]}]

    for account in accounts:
        for region in target_regions:
            # Short-circuit: if instance_ids was provided and we've found them
            # all, stop walking remaining (account, region) pairs.
            if instance_ids and all(i in found for i in instance_ids):
                cache.update(found)
                _request_instance_cache.set(cache)
                found.update(cached_results)
                return found
            try:
                ec2 = get_client('ec2', account_id=account, region=region)
                kwargs: Dict[str, Any] = {'Filters': base_filters}
                if instance_ids:
                    remaining = [i for i in instance_ids if i not in found]
                    if not remaining:
                        cache.update(found)
                        _request_instance_cache.set(cache)
                        found.update(cached_results)
                        return found
                    kwargs['InstanceIds'] = remaining
                resp = ec2.describe_instances(**kwargs)
                for res in resp.get('Reservations', []):
                    for inst in res.get('Instances', []):
                        iid = inst.get('InstanceId')
                        if not iid:
                            continue
                        tags = {t['Key']: t['Value'] for t in inst.get('Tags', [])}
                        found[iid] = {
                            'account_id': account,
                            'region': region,
                            'tags': tags,
                            'state': inst.get('State', {}).get('Name', 'unknown'),
                            'name': tags.get('Name', ''),
                            'instance_type': inst.get('InstanceType', ''),
                        }
            except ClientError as e:
                code = e.response.get('Error', {}).get('Code', '')
                if code == 'InvalidInstanceID.NotFound':
                    # Expected -- this (account, region) doesn't own these IDs.
                    continue
                # Log at ERROR for access/auth failures -- these silently cause
                # false InstanceOutOfScope rejections in _validate_instance_scope.
                if code in ('AccessDenied', 'UnauthorizedOperation', 'AuthFailure'):
                    logger.error(f"[DIRECT_DESCRIBE] AUTH FAILURE {account}/{region}: {code}. "
                                 f"Check PatchySpokeRole trust policy. Instances in this account "
                                 f"will be rejected as out-of-scope.")
                else:
                    logger.warning(f"[DIRECT_DESCRIBE] {account}/{region}: {code} - {e}")
            except Exception as e:
                logger.error(f"[DIRECT_DESCRIBE] {account}/{region} UNEXPECTED: {type(e).__name__}: {e}")

    # Populate request-scoped cache with newly-described instances
    cache.update(found)
    _request_instance_cache.set(cache)

    # Merge cached results back into the return value
    if cached_results:
        found.update(cached_results)

    return found


def _get_fleet_summary(environment: Optional[str] = None) -> dict:
    """Fleet summary via SSM Explorer GetOpsSummary with Resource Data Sync."""
    global _fleet_summary_cache, _fleet_summary_cache_time, _fleet_instances_cache
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
                }

            next_token = resp.get('NextToken')
            if not next_token:
                break

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

        for (account_id, region), iids in by_account_region.items():
            try:
                ec2 = get_client('ec2', account_id=account_id, region=region)
                paginator = ec2.get_paginator('describe_instances')
                for page in paginator.paginate(InstanceIds=iids):
                    for res in page.get('Reservations', []):
                        for inst in res.get('Instances', []):
                            iid = inst['InstanceId']
                            if iid in instances:
                                tags = {t['Key']: t['Value'] for t in inst.get('Tags', [])}
                                instances[iid]['environment'] = tags.get('Environment', 'unknown')
                                instances[iid]['managed'] = tags.get(SCOPE_TAG_KEY) == SCOPE_TAG_VALUE
            except Exception as e:
                logger.warning(f"[FLEET_SUMMARY] Tag lookup failed for {account_id}/{region}: {e}")

            try:
                ssm_client = get_client('ssm', account_id=account_id, region=region)
                for i in range(0, len(iids), 50):
                    batch = iids[i:i + 50]
                    resp = ssm_client.describe_instance_patch_states(InstanceIds=batch)
                    for state in resp.get('InstancePatchStates', []):
                        iid = state['InstanceId']
                        if iid in instances:
                            instances[iid]['missing_count'] = state.get('MissingCount', 0)
                            instances[iid]['installed_count'] = state.get('InstalledCount', 0)
                            instances[iid]['failed_count'] = state.get('FailedCount', 0)
            except Exception as e:
                logger.warning(f"[FLEET_SUMMARY] Patch state lookup failed for {account_id}/{region}: {e}")

        with _fleet_cache_lock:
            _fleet_summary_cache = _aggregate_fleet(instances)
            _fleet_instances_cache = instances
            _fleet_summary_cache_time = now
        logger.info(f"[FLEET_SUMMARY] Cached: {len(instances)} instances, "
                     f"{len(by_account_region)} (account, region) buckets")
        return _filter_fleet_summary(_fleet_summary_cache, environment)

    except Exception as e:
        error_code = getattr(e, 'response', {}).get('Error', {}).get('Code', '') if hasattr(e, 'response') else type(e).__name__
        logger.error(f"[FLEET_SUMMARY] Explorer unavailable ({error_code})")
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
            f"instance(s) unresolved after Explorer + direct describe — "
            f"defaulting to ({hub_id}, {AWS_REGION}). The Automation child for "
            f"these will likely fail with InvalidInstanceId. "
            f"Unresolved: {truly_unresolved[:5]}"
        )

    return groups


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

    cache = _fleet_instances_cache or {}
    # Group describes by (account, region) so we don't reach across accounts
    # uselessly. Use cache when warm; fall back to direct describe for misses
    # (mirrors the hybrid approach in _validate_instance_scope).
    by_loc: Dict[Tuple[str, str], List[str]] = {}
    unknowns: List[str] = []
    for iid in instance_ids:
        cached = cache.get(iid)
        if cached and cached.get('account_id') and cached.get('region'):
            by_loc.setdefault((cached['account_id'], cached['region']), []).append(iid)
        else:
            unknowns.append(iid)
    if unknowns:
        described = _direct_describe_scoped_instances(instance_ids=unknowns)
        for iid, info in described.items():
            by_loc.setdefault((info['account_id'], info['region']), []).append(iid)

    if not by_loc:
        # Couldn't locate any instance -- fall back to env defaults so we
        # still record SOMETHING in the audit trail.
        sla_hours = _DEFAULT_SLA.get(sev, 24)
        return {"sla_hours": sla_hours, "sla_source": "default", "frameworks": [],
                "sla_explanation": f"SLA {sla_hours}h (default — no SLA tag on instances)"}

    strictest_hours: Optional[int] = None
    strictest_source: Optional[str] = None
    all_frameworks: List[str] = []

    for (account, region), iids in by_loc.items():
        try:
            ec2 = get_client('ec2', account_id=account, region=region)
            resp = ec2.describe_instances(InstanceIds=iids)
            for res in resp.get('Reservations', []):
                for inst in res.get('Instances', []):
                    tags = {t['Key']: t['Value'] for t in inst.get('Tags', [])}
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
        except Exception as e:
            logger.warning(f"[SLA_RESOLVE] tag lookup failed for {account}/{region}: {e}")

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
        sla_explanation = f"SLA {strictest_hours}h (default — no SLA tag on instances)"

    return {
        "sla_hours": strictest_hours,
        "sla_source": strictest_source,
        "frameworks": unique_frameworks,
        "sla_explanation": sla_explanation,
    }


def _earliest_window_within_sla(environment: str, instance_ids: List[str],
                                sla_hours: int) -> Optional[Dict[str, Any]]:
    """Return the earliest enabled maintenance window that opens within sla_hours,
    or None if no qualifying window exists.

    Used by patch tools to decide between SCHEDULED and EMERGENCY before
    starting an Automation. Strict -- runs for both Path A and Path B so the
    operator gets the same recommendation regardless of how they targeted
    instances.

    Returns a dict with the window's name, account_id, region, next_execution
    (ISO string), and hours_until_window so the tool can render a useful
    advisory message.
    """
    # Import here to avoid circular import -- get_maintenance_windows is a @tool
    # function that lives in a domain module. When tools are split, this will
    # need to be resolved via a callback or by moving window lookup logic here.
    # For now, we rely on the tool being importable at call time.
    from helper.tools import get_maintenance_windows
    try:
        windows_result = get_maintenance_windows(environment=environment,
                                                  instance_ids=instance_ids)
        windows = windows_result.get('windows') or []
        if not windows:
            return None

        now = datetime.now(timezone.utc)
        best: Optional[Dict[str, Any]] = None
        best_hours: Optional[float] = None

        for w in windows:
            if not w.get('enabled', True):
                continue
            next_exec = w.get('next_execution')
            if not next_exec:
                continue
            try:
                win_dt = datetime.fromisoformat(str(next_exec).replace('Z', '+00:00'))
            except Exception:
                continue
            hours = (win_dt - now).total_seconds() / 3600
            if hours < 0:
                continue
            if hours > sla_hours:
                continue
            if best_hours is None or hours < best_hours:
                best = {
                    'name': w.get('name', 'unknown'),
                    'window_id': w.get('window_id'),
                    'account_id': w.get('account_id'),
                    'region': w.get('region'),
                    'next_execution': str(next_exec),
                    'hours_until_window': round(hours, 2),
                }
                best_hours = hours

        return best
    except Exception as e:
        logger.warning(f"[SLA_WINDOW] earliest-window lookup failed: {e}")
        return None


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


def _earliest_first_observed_at(cve_ids: List[str],
                                account_id: Optional[str] = None,
                                region: Optional[str] = None) -> Optional[str]:
    """Earliest firstObservedAt across ACTIVE Inspector findings for these CVEs.

    Returns an ISO-8601 UTC string, or None when no CVE is supplied or nothing
    is found. This is the vulnerability's discovery time — the start of the SLA
    clock. The earliest observation is the worst case (longest exposure), so a
    multi-instance CVE is judged by its oldest sighting.

    Best-effort: an Inspector error for one CVE is logged and skipped rather
    than raised, so a lookup failure downgrades the SLA to "unknown" instead of
    blocking the patch.
    """
    cves = [c for c in (cve_ids or []) if c and c != 'N/A']
    if not cves:
        return None
    client = _inspector(account_id=account_id, region=region)
    earliest: Optional[datetime] = None
    for cve_id in cves:
        try:
            resp = client.list_findings(
                filterCriteria={
                    'vulnerabilityId': [{'comparison': 'EQUALS', 'value': cve_id}],
                    'findingStatus': [{'comparison': 'EQUALS', 'value': 'ACTIVE'}],
                },
                maxResults=100,
            )
        except Exception as e:
            logger.warning(f"[SLA] Inspector firstObservedAt lookup failed for {cve_id}: {e}")
            continue
        for finding in resp.get('findings', []):
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
        body = {
            'execution_id': execution_id,
            'started_at': datetime.now(timezone.utc).isoformat(),
            'operator': get_operator(),
            **context,  # cve_id, environment, severity, sla, instance_ids, etc.
        }
        _s3().put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(body, default=str).encode('utf-8'),
            ContentType='application/json',
        )
        logger.info(f"[PENDING_CONTEXT] Wrote pending context for {execution_id} to s3://{bucket}/{key}")
    except Exception as e:
        # Best-effort -- don't fail patching if S3 write fails. Reconciliation
        # will use what it can fetch from SSM (just no business context).
        logger.warning(f"[PENDING_CONTEXT] Failed to write pending context for {execution_id}: {e}")


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


def _start_patch_automation(operation: str, environment: str,
                            account_ids: List[str], regions: List[str],
                            max_concurrency: str, max_errors: str,
                            severity_filter: Optional[str] = None) -> str:
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
    params = {
        'Operation': [operation],
        'Environment': [environment],
        'ScopeTagKey': [SCOPE_TAG_KEY],
        'ScopeTagValue': [SCOPE_TAG_VALUE],
        'RebootOption': ['NoReboot'],
        # Layer 3: inner SendCommand fan-out for the tag-based doc.
        'MaxConcurrency': [EXECUTION_DEFAULTS['send_command_max_concurrency']],
        'MaxErrors':      [EXECUTION_DEFAULTS['send_command_max_errors']],
    }
    override_url = _get_baseline_override_url(severity_filter)
    if override_url:
        params['BaselineOverride'] = [override_url]

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
        TargetLocations=target_locations,
    )
    return resp['AutomationExecutionId']


def _start_instance_patch_automation(operation: str, instance_ids: List[str],
                                     account_ids: List[str], regions: List[str],
                                     max_concurrency: str, max_errors: str,
                                     severity_filter: Optional[str] = None) -> str:
    """Start SSM Automation with TargetLocations for instance-ID based patching.

    Uses Patchy-RunPatchBaselineById which targets specific instances by ID
    (--target-parameter-name InstanceId). This gives instance-level precision
    through MAMR without patching all tag-matching instances.

    Returns the parent AutomationExecutionId.
    """
    params: Dict[str, Any] = {
        'Operation': [operation],
        'RebootOption': ['NoReboot'],
        # Layer 3: inner SendCommand. Each Automation iteration targets one
        # instance, so the inner step value is fixed at '1'. Real fan-out
        # comes from Layer 2 (top-level MaxConcurrency below).
        'MaxConcurrency': ['1'],
        'MaxErrors':      ['100%'],
    }
    override_url = _get_baseline_override_url(severity_filter)
    if override_url:
        params['BaselineOverride'] = [override_url]

    target_locations = [{
        'Accounts': account_ids,
        'Regions': regions,
        'ExecutionRoleName': SPOKE_EXECUTION_ROLE,
        'TargetLocationMaxConcurrency': max_concurrency,
        'TargetLocationMaxErrors': max_errors,
    }]

    resp = _ssm().start_automation_execution(
        DocumentName=AUTOMATION_BY_ID_DOC_NAME,
        Parameters=params,
        TargetParameterName='InstanceId',
        Targets=[{'Key': 'ParameterValues', 'Values': instance_ids}],
        TargetLocations=target_locations,
        # Layer 2: how many instance Automation iterations run in parallel
        # inside each (account, region) child execution. Without these,
        # SSM defaults to 1 instance at a time.
        MaxConcurrency=EXECUTION_DEFAULTS['instance_max_concurrency'],
        MaxErrors=EXECUTION_DEFAULTS['instance_max_errors'],
    )
    return resp['AutomationExecutionId']


def _start_instance_rollback_automation(instance_ids: List[str],
                                        account_ids: List[str], regions: List[str],
                                        max_concurrency: str, max_errors: str) -> str:
    """Start SSM Automation with TargetLocations for instance-ID based rollback.

    Uses Patchy-RunRollbackById which targets specific instances by ID.
    Returns the parent AutomationExecutionId.
    """
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
        },
        TargetParameterName='InstanceId',
        Targets=[{'Key': 'ParameterValues', 'Values': instance_ids}],
        TargetLocations=target_locations,
        # Layer 2: instance Automation iterations in parallel per child.
        MaxConcurrency=EXECUTION_DEFAULTS['instance_max_concurrency'],
        MaxErrors=EXECUTION_DEFAULTS['instance_max_errors'],
    )
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
    # Imports (re-exported for convenience)
    'boto3', 'Config', 'ClientError', 'contextvars', 'json', 'logging',
    'os', 're', 'threading', 'time',
    'datetime', 'timedelta', 'timezone',
    'Any', 'Dict', 'List', 'Optional', 'Tuple',

    # Re-exports from cross_account
    'is_multi_account', 'get_client', 'get_hub_account_id',
    'resolve_scope', 'fan_out', 'build_target_locations', 'format_execution_plan',
    'EXECUTION_DEFAULTS', 'SPOKE_EXECUTION_ROLE', 'SPOKE_REGIONS',

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
    'set_operator', 'get_operator',
    'set_timezone', 'get_timezone',
    'clear_request_scans',

    # Time formatting
    '_format_utc_as_local',

    # AWS client factories
    '_ssm', '_ec2', '_s3', '_inspector', '_cloudwatch',

    # Validation helpers
    '_validate_instance_ids',
    '_validate_instance_scope',
    '_normalize_environment',

    # Cross-account query targets
    '_configured_scope_cache',
    '_resolve_ou_member_accounts',
    '_get_configured_scope_accounts',
    '_validate_account_scope',
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

    # Baseline override helpers
    '_get_baseline_override_url',

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
