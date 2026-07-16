"""Cross-account client factory and scope resolution.

Provides STS assume-role with thread-safe credential caching, Organizations-based
account discovery, bounded-concurrency fan-out across accounts, and SSM
TargetLocations builder for cross-account command execution.

Single-account mode: when MULTI_ACCOUNT_ENABLED is not set, all functions
gracefully degrade — resolve_scope returns the hub account only, get_client
returns a standard boto3 client, and build_target_locations returns None.
"""

import boto3
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from typing import Optional, Dict, List, Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar('T')

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

    # Aliases used by format_execution_plan when an operator passes overrides.
    # When unset, they fall back to the account-level values above.
    'max_concurrency': '75%',
    'max_errors':      '100%',
}

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
        key = f"{account_id}:{region}"
        now = time.time()

        with self._lock:
            cached = self._cache.get(key)
            if cached and cached['expiry'] - now > 300:  # 5min buffer
                return cached['credentials']

        # Assume role outside the lock (network call)
        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
        sts = boto3.client('sts', region_name=AWS_REGION, config=_aws_config)
        try:
            resp = sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName=f"patchy-{account_id}-{region}",
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


# ============================================================================
# CLIENT FACTORY
# ============================================================================

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
        return boto3.client(service, region_name=region, config=_aws_config)

    creds = _credential_cache.get_credentials(account_id, region, role_name)
    return boto3.client(service, region_name=region, config=_aws_config, **creds)


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
        accounts = []
        for ou_id in ou_ids:
            paginator = org_client.get_paginator('list_accounts_for_parent')
            for page in paginator.paginate(ParentId=ou_id):
                accounts.extend(page['Accounts'])
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

def fan_out(func: Callable[..., T],
            accounts: List[Dict[str, Any]],
            max_workers: int = _READ_FAN_OUT_WORKERS) -> List[Dict[str, Any]]:
    """Execute a function across accounts with bounded concurrency.

    func receives (account_id, regions, **account_dict) and should return a dict.
    Results are collected per-account with success/failure status.
    """
    results = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for acct in accounts:
            future = executor.submit(
                func,
                account_id=acct['account_id'],
                regions=acct.get('regions', [AWS_REGION]),
            )
            futures[future] = acct['account_id']

        for future in as_completed(futures):
            account_id = futures[future]
            try:
                result = future.result()
                results.append({
                    'account_id': account_id,
                    'status': 'success',
                    'data': result,
                })
            except Exception as e:
                logger.error(f"[CROSS_ACCOUNT] Fan-out failed for {account_id}: {e}")
                results.append({
                    'account_id': account_id,
                    'status': 'error',
                    'error': str(e),
                })

    succeeded = sum(1 for r in results if r['status'] == 'success')
    logger.info(f"[CROSS_ACCOUNT] Fan-out complete: {succeeded}/{len(accounts)} succeeded")
    return results


# ============================================================================
# SSM TARGET LOCATIONS BUILDER
# ============================================================================

def build_target_locations(account_ids: List[str],
                           regions: List[str],
                           max_concurrency: Optional[str] = None,
                           max_errors: Optional[str] = None,
                           role_name: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """Build SSM TargetLocations for cross-account command execution.

    Returns None in single-account mode (caller uses standard send_command).
    Returns the TargetLocations list for multi-account send_command/register_task.
    """
    if not account_ids or len(account_ids) <= 1:
        # Single account — no TargetLocations needed, caller uses standard path
        if len(account_ids) == 1 and account_ids[0] == get_hub_account_id():
            return None

    role = role_name or SPOKE_EXECUTION_ROLE
    concurrency = max_concurrency or EXECUTION_DEFAULTS['account_max_concurrency']
    errors = max_errors or EXECUTION_DEFAULTS['account_max_errors']

    return [{
        'Accounts': account_ids,
        'Regions': regions,
        'ExecutionRoleName': role,
        'TargetLocationMaxConcurrency': concurrency,
        'TargetLocationMaxErrors': errors,
    }]


def format_execution_plan(scope: Dict[str, Any],
                          operation: str,
                          max_concurrency: Optional[str] = None,
                          max_errors: Optional[str] = None) -> Dict[str, Any]:
    """Build an execution plan summary for operator confirmation.

    The agent MUST present this to the operator and wait for confirmation
    before executing any cross-account write operation (patch, rollback, etc.).

    Returns a structured plan the agent can render as a confirmation prompt,
    including the command_id field (populated after execution).
    """
    defaults = EXECUTION_DEFAULTS
    concurrency = max_concurrency or defaults['max_concurrency']
    errors = max_errors or defaults['max_errors']

    return {
        'operation': operation,
        'total_accounts': scope['total_accounts'],
        'regions': scope['regions'],
        'account_ids': [a['account_id'] for a in scope['accounts']],
        'max_concurrency': concurrency,
        'max_errors': errors,
        'account_max_concurrency': defaults['account_max_concurrency'],
        'account_max_errors': defaults['account_max_errors'],
        'instance_max_concurrency': defaults['instance_max_concurrency'],
        'instance_max_errors': defaults['instance_max_errors'],
        'confirmation_required': True,
        'command_id': None,  # Populated after execution
    }
