"""Fleet discovery and scope resolution tools."""

from strands import tool
from typing import Optional, List, Dict, Any

from . import _shared
from ._shared import (
    logger, os,
    classify_error,
    ClientError,
    get_client, get_hub_account_id,
    resolve_scope,
    _normalize_environment,
    _get_configured_scope_accounts,
    _get_query_targets,
    _get_fleet_summary,
    _direct_describe_scoped_instances,
    AWS_REGION,
    SCOPE_TAG_KEY, SCOPE_TAG_VALUE,
    EXECUTION_DEFAULTS, SPOKE_REGIONS,
)


# ============================================================================
# TOOLS
# ============================================================================


@tool
def get_fleet_overview(environment: Optional[str] = None,
                       account_id: Optional[str] = None) -> dict:
    """Fleet overview via SSM Explorer + direct EC2 describe (hybrid). Cross-account, cross-region.

    Explorer (Resource Data Sync) is the bulk source -- it has tags, patch
    state, and SSM-managed status for every instance it knows about. It lags
    real-world state by 30 min to ~1 day. We supplement with direct EC2
    describe across configured (account, region) so newly launched instances
    show up before Explorer ingests them. Pending-ingestion rows are tagged
    with `data_source='ec2_direct'` and have `missing_patches=None` since we
    can't read patch state without Explorer.

    Args:
        environment: Filter by environment (dev, staging, prod). None=all.
        account_id: Restrict to a specific account (e.g., '111122223333').
                    Only pass when the user explicitly names an account.
                    Must be in the configured scope. None = all accounts.

    Returns:
        dict: {environments, accounts, instances_by_environment, instance_count,
               explorer_available, pending_ingestion_count}
    """
    try:
        # Validate account_id against configured scope
        if account_id:
            allowed = _get_configured_scope_accounts()
            if allowed and account_id not in allowed:
                return {
                    "error": f"Account {account_id} is not in the configured scope. "
                             f"Allowed accounts: {sorted(allowed)}.",
                    "instances_by_environment": {},
                    "instance_count": 0,
                    "explorer_available": True,
                }

        env_value = _normalize_environment(environment) if environment else None
        fleet = _get_fleet_summary(environment=env_value)

        instances_by_env: Dict[str, list] = {}
        seen_ids: set = set()
        for iid, inst in (_shared._fleet_instances_cache or {}).items():
            if not inst.get('managed'):
                continue
            if env_value and inst.get('environment') != env_value:
                continue
            # Filter by account_id when specified
            if account_id and inst.get('account_id') != account_id:
                continue
            env = inst.get('environment', 'unknown')
            instances_by_env.setdefault(env, []).append({
                'instance_id': iid,
                'name': inst.get('name', ''),
                'account_id': inst.get('account_id', 'unknown'),
                'status': inst.get('ping_status', 'Unknown'),
                'missing_patches': inst.get('missing_count', 0),
                'data_source': 'explorer',
            })
            seen_ids.add(iid)

        # Hybrid: direct EC2 describe to surface instances not yet ingested
        # by Explorer. Same scope-tag filter, so untagged instances stay
        # invisible. Marked `data_source='ec2_direct'` so the LLM and
        # downstream tools know the row is pending Explorer enrichment.
        accounts_arg = [account_id] if account_id else None
        direct = _direct_describe_scoped_instances(accounts=accounts_arg)
        pending_count = 0
        for iid, info in direct.items():
            if iid in seen_ids:
                continue
            if info.get('state') not in ('running', 'pending', 'stopping', 'stopped'):
                continue
            inst_env = (info.get('tags') or {}).get('Environment', 'unknown')
            if env_value and inst_env != env_value:
                continue
            instances_by_env.setdefault(inst_env, []).append({
                'instance_id': iid,
                'name': info.get('name', ''),
                'account_id': info.get('account_id', 'unknown'),
                'status': info.get('state', 'Unknown'),
                'missing_patches': None,
                'data_source': 'ec2_direct',
                'note': ('Pending fleet sync. The instance is running and visible '
                         'in EC2 + Inspector, but has not yet been aggregated into '
                         'the Resource Data Sync that powers the fleet view '
                         '(typical lag: 30 min to several hours after launch). '
                         'Patch state will appear once the sync ingests it.'),
            })
            pending_count += 1

        total = sum(len(v) for v in instances_by_env.values())
        logger.info(f"[TOOL:get_fleet_overview] env={env_value} account_id={account_id} "
                    f"total={total} explorer={total - pending_count} pending={pending_count}")

        # Build summary
        _s_env_breakdown = ', '.join(f"{len(v)} {k}" for k, v in sorted(instances_by_env.items(), key=lambda x: len(x[1]), reverse=True))
        _s_non_compliant = sum(1 for envlist in instances_by_env.values() for inst in envlist if (inst.get('missing_patches') or 0) > 0)
        _s_summary = f"{total} instances ({_s_env_breakdown}). {_s_non_compliant} non-compliant. {pending_count} pending ingestion." if total > 0 else "0 instances found."

        result = {
            'summary': _s_summary,
            'environments': fleet.get('environments', {}),
            'accounts': fleet.get('accounts', {}),
            'instances_by_environment': instances_by_env,
            'instance_count': total,
            'pending_ingestion_count': pending_count,
            'explorer_available': fleet.get('explorer_available', False),
            'next_action': "Present the fleet overview using the fleet_overview response template.",
        }
        if pending_count > 0:
            result['discovery_note'] = (
                f"{pending_count} instance(s) found via direct EC2 only (not yet in Explorer). "
                "Patch state unavailable until Explorer sync completes (typically 30 min to several hours)."
            )
        return result
    except Exception as e:
        logger.error(f"[TOOL:get_fleet_overview] ERROR: {e}")
        return classify_error(e)


@tool
def resolve_execution_scope(environment: str,
                            account_ids: Optional[List[str]] = None,
                            regions: Optional[List[str]] = None,
                            ou_ids: Optional[List[str]] = None) -> dict:
    """Resolve accounts and count instances for a cross-account operation.

    Decision: Always call before multi_account_dry_run or multi_account_execute.

    Args:
        environment: Target environment (dev, staging, prod)
        account_ids: Explicit account IDs (skips discovery)
        regions: Target regions (default: hub region)
        ou_ids: Organizations OU IDs for account discovery

    Returns:
        dict: {accounts, total_accounts, total_instances, execution_defaults, confirmation_required}
    """
    try:
        env_value = _normalize_environment(environment)
        logger.info(f"[TOOL:resolve_execution_scope] env={env_value} accounts={account_ids} "
                     f"regions={regions} ou_ids={ou_ids}")

        scope = resolve_scope(environment=env_value, account_ids=account_ids,
                              regions=regions, ou_ids=ou_ids)

        fleet = _get_fleet_summary(environment=env_value)
        fleet_accounts = fleet.get('accounts', {})

        # Get per-environment instance counts from the fleet cache
        # fleet_accounts has totals across ALL environments per account.
        # For accurate per-environment counts, filter from the instance cache.
        env_instance_counts: Dict[str, int] = {}
        env_missing_counts: Dict[str, int] = {}
        if _shared._fleet_instances_cache and env_value:
            for iid, inst in _shared._fleet_instances_cache.items():
                if not inst.get('managed'):
                    continue
                if inst.get('environment') != env_value:
                    continue
                acct = inst.get('account_id', '')
                env_instance_counts[acct] = env_instance_counts.get(acct, 0) + 1
                env_missing_counts[acct] = env_missing_counts.get(acct, 0) + inst.get('missing_count', 0)

        accounts = []
        total_instances = 0
        scope_account_ids = [s['account_id'] for s in scope['accounts']]
        total_missing = 0
        total_unmanaged = 0
        for acct_id in scope_account_ids:
            acct_data = fleet_accounts.get(acct_id, {})
            # Use environment-filtered counts if available, otherwise fall back to account totals
            if env_value and env_instance_counts:
                count = env_instance_counts.get(acct_id, 0)
                missing = env_missing_counts.get(acct_id, 0)
            else:
                count = acct_data.get('managed', 0)
                missing = acct_data.get('missing_patches', 0)
            unmanaged = acct_data.get('unmanaged', 0)
            total_instances += count
            total_missing += missing
            total_unmanaged += unmanaged
            accounts.append({
                'account_id': acct_id,
                'instance_count': count,
                'unmanaged_count': unmanaged,
                'online': acct_data.get('online', 0),
                'offline': acct_data.get('offline', 0),
                'missing_patches': missing,
                'compliant_instances': acct_data.get('compliant_instances', 0),
                'scanned_instances': acct_data.get('scanned_instances', 0),
            })

        accounts.sort(key=lambda a: a.get('instance_count', 0), reverse=True)

        logger.info(f"[TOOL:resolve_execution_scope] RESULT: {len(accounts)} accounts, "
                     f"{total_instances} managed, {total_unmanaged} unmanaged, {total_missing} missing")

        _s_summary = f"{len(accounts)} accounts, {total_instances} managed instances, {total_missing} missing patches." if accounts else "0 accounts found."

        return {
            'summary': _s_summary,
            'environment': env_value,
            'accounts': accounts,
            'total_accounts': len(accounts),
            'total_instances': total_instances,
            'total_unmanaged': total_unmanaged,
            'total_missing': total_missing,
            'regions': scope['regions'],
            'multi_account': scope['multi_account'],
            'execution_defaults': EXECUTION_DEFAULTS,
            'confirmation_required': scope['multi_account'],
            'next_action': "Present the account plan to the operator. Then call multi_account_dry_run or multi_account_execute with these account_ids.",
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"[TOOL:resolve_execution_scope] ERROR: {error_info}")
        return error_info
    except Exception as e:
        logger.error(f"[TOOL:resolve_execution_scope] ERROR: {e}")
        return classify_error(e)
