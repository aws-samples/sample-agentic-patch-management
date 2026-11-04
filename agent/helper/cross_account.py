"""Cross-account client factory and scope resolution.

Provides STS assume-role with thread-safe credential caching, Organizations-based
account discovery (child OUs included), scope resolution, and the rollout limits
per environment.

Single-account mode: when MULTI_ACCOUNT_ENABLED is not set, all functions
gracefully degrade — resolve_scope returns the hub account only, and
get_client returns a standard boto3 client.
"""

import boto3
import logging
import os
import re
import threading
import time
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)


AWS_REGION = os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))
MULTI_ACCOUNT_ENABLED = os.environ.get('MULTI_ACCOUNT_ENABLED', '').lower() == 'true'
SPOKE_EXECUTION_ROLE = os.environ.get('SPOKE_EXECUTION_ROLE', 'PatchySpokeRole')
SPOKE_REGIONS = [r.strip() for r in os.environ.get('SPOKE_REGIONS', AWS_REGION).split(',') if r.strip()]

# Retry config shared across all cross-account clients
_aws_config = BotoConfig(
    retries={'max_attempts': 3, 'mode': 'adaptive'},
    read_timeout=180,
    connect_timeout=10,
)

# Defaults the agent presents to the operator before cross-account execution.
# Operator must confirm or override — tools never execute without confirmation.
#
# Two layers in SSM Automation:
#   - Account-level (TargetLocations): how many (account, region) child
#     executions run in parallel and how many of those may fail.
#   - Instance-level (top-level MaxConcurrency on the parent Automation):
#     how many instances inside each child run in parallel.
#
# Both layers default to fully parallel for fast fan-out at scale. Tighten
# per-environment via the operator's confirm flow when blast-radius matters.
EXECUTION_DEFAULTS = {
    # ── Layer 1: Account-level (TargetLocations) ────────────────────
    # How many (account, region) child executions run in parallel and how
    # many of those children may fail before SSM aborts the parent.
    'account_max_concurrency': '75%',
    'account_max_errors':      '100%',

    # ── Layer 2: Instance-level (top-level on the parent Automation) ─
    # ONLY applies to instance-ID docs (RunPatchBaselineById /
    # RunRollbackById). Controls how many instance iterations run in
    # parallel inside a child execution. Tag-based docs have no
    # TargetParameterName so SSM rejects this — it's silently ignored
    # by the helper for tag-based starts.
    'instance_max_concurrency': '50%',
    'instance_max_errors':      '100%',

    # ── Layer 3: Inner SendCommand (aws:runCommand inside the doc) ──
    # Applies to BOTH instance-ID and tag-based docs, but the values
    # mean different things:
    #   - Tag-based docs: how many tagged instances run at once across
    #     the resolved tag query. This is the real fan-out lever for
    #     tag-based execution.
    #   - Instance-ID docs: each Automation iteration already targets
    #     exactly one instance, so '1' is the only meaningful value
    #     here. Customers tuning fleet-wide parallelism should change
    #     'instance_max_concurrency' (Layer 2) instead.
    'send_command_max_concurrency': '50%',
    'send_command_max_errors':      '100%',

    # Generic aliases for callers that take max_concurrency/max_errors; when unset
    # they fall back to the account-level values above.
    'max_concurrency': '75%',
    'max_errors':      '100%',
}

# Rollout limits for changes (install and rollback), by environment. They stop
# a run as soon as errors pass the threshold instead of letting a bad patch
# reach every instance. Environments not listed get the prod limits. Scans
# change nothing and keep EXECUTION_DEFAULTS.
ENVIRONMENT_LIMITS = {
    'dev': {
        'account_max_concurrency': '100%', 'account_max_errors': '25%',
        'instance_max_concurrency': '50%', 'instance_max_errors': '25%',
        'send_command_max_concurrency': '50%', 'send_command_max_errors': '25%',
    },
    'staging': {
        'account_max_concurrency': '50%', 'account_max_errors': '1',
        'instance_max_concurrency': '25%', 'instance_max_errors': '1',
        'send_command_max_concurrency': '25%', 'send_command_max_errors': '1',
    },
    'prod': {
        'account_max_concurrency': '1', 'account_max_errors': '0',
        'instance_max_concurrency': '10%', 'instance_max_errors': '0',
        'send_command_max_concurrency': '10%', 'send_command_max_errors': '0',
    },
}


REBOOT_OPTIONS = ('NoReboot', 'RebootIfNeeded')


def reboot_option(environment: Optional[str]) -> str:
    """Reboot setting for an install in an environment.

    REBOOT_POLICY lists environments that may reboot, e.g.
    "dev=RebootIfNeeded,staging=RebootIfNeeded". Anything not listed, or set to
    an unknown value, doesn't reboot.
    """
    policy = {}
    for item in os.environ.get('REBOOT_POLICY', '').split(','):
        env, _, value = item.partition('=')
        if value.strip() in REBOOT_OPTIONS:
            policy[env.strip().lower()] = value.strip()
    return policy.get((environment or '').lower(), 'NoReboot')


def execution_limits(environment: Optional[str]) -> dict:
    """Rollout limits for a change in an environment.

    Args:
        environment: Normalised environment, or None for operations that change nothing.

    Returns:
        EXECUTION_DEFAULTS overlaid with the environment's limits (prod's when unknown).
    """
    if not environment:
        return dict(EXECUTION_DEFAULTS)
    return {**EXECUTION_DEFAULTS, **ENVIRONMENT_LIMITS.get(environment.lower(), ENVIRONMENT_LIMITS['prod'])}


# Fan-out concurrency for assume-role read paths (hidden from operator)
_READ_FAN_OUT_WORKERS = 10


# ============================================================================
# HUB IDENTITY
# ============================================================================

_hub_account_id: Optional[str] = None
_hub_lock = threading.Lock()


def get_hub_account_id() -> str:
    """Return the hub (current) account ID. Cached after first call."""
    global _hub_account_id
    if _hub_account_id is None:
        with _hub_lock:
            if _hub_account_id is None:
                sts = boto3.client('sts', region_name=AWS_REGION, config=_aws_config)
                _hub_account_id = sts.get_caller_identity()['Account']
    return _hub_account_id


def is_multi_account() -> bool:
    return MULTI_ACCOUNT_ENABLED


# ============================================================================
# STS CREDENTIAL CACHE
# ============================================================================

def _session_name(operator: str, account_id: str, region: str) -> str:
    """RoleSessionName naming the operator, so spoke CloudTrail shows who a call was for."""
    who = re.sub(r"[^\w+=,.@-]", "-", operator or "unknown").strip("-") or "unknown"
    return f"patchy-{who}"[:64 - 14] + f"-{account_id}"[:14]


def _current_operator() -> str:
    try:
        from helper.tools._shared import get_operator
        return get_operator()
    except Exception:
        return "unknown"


class _CredentialCache:
    """Thread-safe STS credential cache with expiry-aware refresh.

    Caches assumed-role credentials per (account_id, region) key.
    Refreshes 5 minutes before expiry to avoid mid-call expirations.
    """

    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get_credentials(self, account_id: str, region: str,
                        role_name: str = SPOKE_EXECUTION_ROLE) -> Dict[str, str]:
        operator = _current_operator()
        # Keyed by role and operator too: the read and write roles must not share
        # credentials, and each session name carries its operator.
        key = f"{role_name}:{account_id}:{region}:{operator}"
        now = time.time()

        with self._lock:
            cached = self._cache.get(key)
            if cached and cached['expiry'] - now > 300:  # 5min buffer
                return cached['credentials']

        # Assume role outside the lock (network call)
        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
        try:
            resp = _sts_client().assume_role(
                RoleArn=role_arn,
                RoleSessionName=_session_name(operator, account_id, region),
                DurationSeconds=3600,
            )
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            logger.error(f"[CROSS_ACCOUNT] Failed to assume role {role_arn}: {error_code}")
            raise

        creds = resp['Credentials']
        entry = {
            'credentials': {
                'aws_access_key_id': creds['AccessKeyId'],
                'aws_secret_access_key': creds['SecretAccessKey'],
                'aws_session_token': creds['SessionToken'],
            },
            'expiry': creds['Expiration'].timestamp(),
        }

        with self._lock:
            self._cache[key] = entry

        logger.info(f"[CROSS_ACCOUNT] Assumed role in {account_id}/{region}")
        return entry['credentials']

    def clear(self):
        with self._lock:
            self._cache.clear()


_credential_cache = _CredentialCache()

# One hub STS client for every assume-role (building a client per credential miss was costly).
_sts = None
_sts_lock = threading.Lock()


def _sts_client():
    global _sts
    with _sts_lock:
        if _sts is None:
            _sts = boto3.client('sts', region_name=AWS_REGION, config=_aws_config)
        return _sts


# ============================================================================
# CLIENT FACTORY
# ============================================================================

# boto3 clients are thread-safe and slow to build; reuse them per service,
# region and credentials.
_client_cache: Dict[tuple, Any] = {}
_client_cache_lock = threading.Lock()


def get_client(service: str, account_id: Optional[str] = None,
               region: Optional[str] = None,
               role_name: Optional[str] = None) -> Any:
    """Get a boto3 client, optionally for a spoke account via STS assume-role.

    When account_id is None or matches the hub, returns a standard client
    using current credentials. Otherwise assumes the spoke role.
    """
    region = region or AWS_REGION
    role_name = role_name or SPOKE_EXECUTION_ROLE

    # Hub account — no assume-role needed
    if not account_id or account_id == get_hub_account_id():
        key = (service, region, None, None)
        creds = {}
    else:
        creds = _credential_cache.get_credentials(account_id, region, role_name)
        # Keyed on the session token so a refreshed role session gets a new client.
        key = (service, region, account_id, creds.get('aws_session_token'))
    client = _client_cache.get(key)
    if client is None:
        # Built under the lock: concurrent first uses would each build one, and boto3's
        # default session isn't safe to build clients from in parallel.
        with _client_cache_lock:
            client = _client_cache.get(key)
            if client is None:
                client = boto3.client(service, region_name=region, config=_aws_config, **creds)
                # One client per service, region and account: a refreshed role
                # session replaces the client built on the expired one.
                for old in [k for k in _client_cache if k[:3] == key[:3] and k != key]:
                    del _client_cache[old]
                _client_cache[key] = client
    return client


# ============================================================================
# ORGANIZATIONS DISCOVERY
# ============================================================================

def discover_accounts(ou_ids: Optional[List[str]] = None,
                      tag_filter: Optional[Dict[str, str]] = None) -> List[Dict[str, str]]:
    """Discover active accounts from AWS Organizations.

    Returns list of {'account_id': str, 'name': str, 'email': str}.
    Filters by OU membership and/or account tags if provided.
    """
    org_client = boto3.client('organizations', region_name='us-east-1', config=_aws_config)

    if ou_ids:
        # An OU's accounts include those in its child OUs, as in the console.
        accounts = []
        pending, seen = list(ou_ids), set()
        while pending:
            ou_id = pending.pop()
            if ou_id in seen:
                continue
            seen.add(ou_id)
            for page in org_client.get_paginator('list_accounts_for_parent').paginate(ParentId=ou_id):
                accounts.extend(page['Accounts'])
            for page in org_client.get_paginator('list_organizational_units_for_parent').paginate(ParentId=ou_id):
                pending.extend(ou['Id'] for ou in page.get('OrganizationalUnits', []))
    else:
        paginator = org_client.get_paginator('list_accounts')
        accounts = []
        for page in paginator.paginate():
            accounts.extend(page['Accounts'])

    # Only active accounts, exclude hub
    hub_id = get_hub_account_id()
    result = [
        {'account_id': a['Id'], 'name': a.get('Name', ''), 'email': a.get('Email', '')}
        for a in accounts
        if a['Status'] == 'ACTIVE' and a['Id'] != hub_id
    ]

    if tag_filter:
        result = _filter_accounts_by_tags(org_client, result, tag_filter)

    logger.info(f"[CROSS_ACCOUNT] Discovered {len(result)} spoke accounts")
    return result


def _filter_accounts_by_tags(org_client, accounts: List[Dict],
                             tag_filter: Dict[str, str]) -> List[Dict]:
    """Filter accounts by Organizations tags."""
    filtered = []
    for acct in accounts:
        try:
            resp = org_client.list_tags_for_resource(ResourceId=acct['account_id'])
            tags = {t['Key']: t['Value'] for t in resp.get('Tags', [])}
            if all(tags.get(k) == v for k, v in tag_filter.items()):
                filtered.append(acct)
        except ClientError:
            continue  # Skip accounts we can't read tags for
    return filtered


# ============================================================================
# SCOPE RESOLUTION
# ============================================================================

def _scope_request_error(account_ids: Optional[List[str]], regions: Optional[List[str]],
                         ou_ids: Optional[List[str]]) -> Optional[Dict[str, Any]]:
    """An error when a scope request names accounts, regions or OUs outside the configured scope."""
    configured_regions = SPOKE_REGIONS if MULTI_ACCOUNT_ENABLED else [AWS_REGION]
    bad_regions = [r for r in regions or [] if r not in configured_regions]
    configured_ous = [o.strip() for o in os.environ.get('SPOKE_OU_IDS', '').split(',') if o.strip()]
    bad_ous = [o for o in ou_ids or [] if o not in configured_ous]
    bad_accounts: List[str] = []
    if account_ids:
        hub = get_hub_account_id()
        if not MULTI_ACCOUNT_ENABLED:
            allowed = {hub}
        elif configured_ous:
            # The cached scope, not a fresh walk of the OU tree on every request.
            from helper.tools._shared import _get_configured_scope_accounts
            allowed = set(_get_configured_scope_accounts()) | {hub}
        else:
            allowed = {a.strip() for a in os.environ.get('SPOKE_ACCOUNT_IDS', '').split(',') if a.strip()} | {hub}
        bad_accounts = [a for a in account_ids if a not in allowed]
    if not (bad_regions or bad_ous or bad_accounts):
        return None
    parts = [f"accounts {bad_accounts}" if bad_accounts else "", f"regions {bad_regions}" if bad_regions else "",
             f"OUs {bad_ous}" if bad_ous else ""]
    return {"error": "Outside the configured scope: " + "; ".join(p for p in parts if p) + ".",
            "error_code": "OutOfScope", "category": "ABORT", "retryable": False,
            "next_action": "Use the configured accounts, regions and OUs (SPOKE_ACCOUNT_IDS, SPOKE_OU_IDS, "
                           "SPOKE_REGIONS)."}


def resolve_scope(environment: Optional[str] = None,
                  account_ids: Optional[List[str]] = None,
                  regions: Optional[List[str]] = None,
                  ou_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Resolve a targeting request into concrete accounts and regions.

    Priority: explicit account_ids > ou_ids > environment-based discovery > hub only.

    Returns:
        {
            'accounts': [{'account_id': str, 'regions': [str], ...}],
            'total_accounts': int,
            'regions': [str],
            'multi_account': bool,
            'execution_defaults': {...},
        }
    """
    refused = _scope_request_error(account_ids, regions, ou_ids)
    if refused:
        return refused
    if not MULTI_ACCOUNT_ENABLED:
        return {
            'accounts': [{'account_id': get_hub_account_id(), 'regions': [AWS_REGION]}],
            'total_accounts': 1,
            'regions': [AWS_REGION],
            'multi_account': False,
            'execution_defaults': EXECUTION_DEFAULTS,
        }

    resolved_regions = regions or SPOKE_REGIONS
    try:
        hub_id = get_hub_account_id()
    except Exception:
        hub_id = None

    # Explicit account list
    if account_ids:
        accounts = [{'account_id': aid, 'regions': resolved_regions} for aid in account_ids]
    # OU-based discovery
    elif ou_ids:
        discovered = discover_accounts(ou_ids=ou_ids)
        accounts = [{'account_id': a['account_id'], 'name': a['name'],
                      'regions': resolved_regions} for a in discovered]
        # Include hub if it's in the OUs
        if hub_id and not any(a['account_id'] == hub_id for a in accounts):
            accounts.insert(0, {'account_id': hub_id, 'regions': resolved_regions})
    # Use configured scope (SPOKE_ACCOUNT_IDS or SPOKE_OU_IDS from .env)
    else:
        # Honor the operator's configured scope — never discover all org accounts
        # without explicit OU or account targeting (would include management, audit, etc.)
        spoke_account_ids = [a.strip() for a in os.environ.get('SPOKE_ACCOUNT_IDS', '').split(',') if a.strip()]
        spoke_ou_ids = [o.strip() for o in os.environ.get('SPOKE_OU_IDS', '').split(',') if o.strip()]

        if spoke_ou_ids:
            discovered = discover_accounts(ou_ids=spoke_ou_ids)
            accounts = [{'account_id': a['account_id'], 'name': a.get('name', ''),
                          'regions': resolved_regions} for a in discovered]
            # Include hub if not already in OU-discovered list
            if hub_id and not any(a['account_id'] == hub_id for a in accounts):
                accounts.insert(0, {'account_id': hub_id, 'regions': resolved_regions})
        elif spoke_account_ids:
            # Use the explicit list (may already include hub)
            accounts = [{'account_id': aid, 'regions': resolved_regions} for aid in spoke_account_ids]
            # Include hub if not already in the list
            if hub_id and not any(a['account_id'] == hub_id for a in accounts):
                accounts.insert(0, {'account_id': hub_id, 'regions': resolved_regions})
        else:
            # Neither configured — fall back to hub only (safe default)
            accounts = [{'account_id': hub_id or get_hub_account_id(), 'regions': resolved_regions}]

    return {
        'accounts': accounts,
        'total_accounts': len(accounts),
        'regions': resolved_regions,
        'multi_account': len(accounts) > 1,
        'execution_defaults': EXECUTION_DEFAULTS,
    }


# ============================================================================
# BOUNDED FAN-OUT
# ============================================================================

# ============================================================================
# SSM TARGET LOCATIONS BUILDER
# ============================================================================

