"""Fleet discovery and scope resolution tools."""

from strands import tool

from helper.display import fleet_card
from typing import Optional, List, Dict

from . import _shared
from ._shared import (
    logger, classify_error,
    ClientError,
    resolve_scope,
    _normalize_environment,
    _get_configured_scope_accounts,
    _get_fleet_summary,
    _direct_describe_scoped_instances,
    execution_limits,
)


# ============================================================================
# TOOLS
# ============================================================================


# Instances per environment the model sees; the card and counts cover all of them.
MODEL_INSTANCES_PER_ENV = 15


def _env_matches(tag_value: Optional[str], environment: str) -> bool:
    """Whether an instance's Environment tag means this environment (Production is prod)."""
    return bool(tag_value) and _normalize_environment(tag_value) == environment


def _instances_for_model(instances_by_env: Dict[str, list]) -> Dict[str, dict]:
    """Per environment: counts plus the instances most worth mentioning (missing patches first)."""
    out = {}
    for env, insts in instances_by_env.items():
        ranked = sorted(insts, key=lambda i: -(i.get('missing_patches') or 0))
        out[env] = {
            'count': len(insts),
            'with_missing_patches': sum(1 for i in insts if (i.get('missing_patches') or 0) > 0),
            'pending_sync': sum(1 for i in insts if i.get('data_source') == 'ec2_direct'),
            'instances': ranked[:MODEL_INSTANCES_PER_ENV],
        }
        if len(insts) > MODEL_INSTANCES_PER_ENV:
            out[env]['instances_omitted'] = len(insts) - MODEL_INSTANCES_PER_ENV
    return out


@tool
def get_fleet_overview(environment: Optional[str] = None,
                       account_id: Optional[str] = None) -> dict:
    """Instances in scope by environment and account, with patch state.

    Use for inventory and broad questions: "show instances", "overall security posture".

    Args:
        environment: dev, staging or prod. Omit for all.
        account_id: Only when the operator names an account.
    """
    # Explorer (Resource Data Sync) is the bulk source but lags 30 min to a day, so
    # direct EC2 describes fill in new instances (data_source='ec2_direct',
    # missing_patches=None until Explorer ingests them).
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
        for iid, inst in _shared.fleet_instances().items():
            if not inst.get('managed'):
                continue
            if env_value and not _env_matches(inst.get('environment'), env_value):
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
                # None when Systems Manager has no patch state for it: not scanned, not "0 missing".
                'missing_patches': inst.get('missing_count', 0) if inst.get('scanned', True) else None,
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
            if env_value and not _env_matches(inst_env, env_value):
                continue
            inst_env = _normalize_environment(inst_env) if inst_env != 'unknown' else inst_env
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
                         'Patch state will appear once the sync ingests it. Plans '
                         'and runs for its environment already include it: a run '
                         'targets the Environment tag, not the fleet view.'),
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
            'instances_by_environment': _instances_for_model(instances_by_env),
            'instance_count': total,
            'pending_ingestion_count': pending_count,
            'explorer_available': fleet.get('explorer_available', False),
            'next_action': "Summarise the fleet by environment.",
            'display': fleet_card(instances_by_env, env_value),
        }
        # Locations whose instances couldn't be read are missing from these counts (disruption
        # round, 3 Nov: an IAM deny in one Region left dev at "4 instances, all online").
        unreadable = _shared.location_names(list(_shared.fleet_view_unreadable()) + list(getattr(direct, 'unreadable', None) or []))
        if unreadable:
            result['unreadable_locations'] = unreadable
            result['next_action'] = (f"Say first that instances in {', '.join(unreadable[:3])} couldn't be read, "
                                     "so they aren't in these counts and the fleet may be larger. "
                                     + result['next_action'])
            card = result.get('display')
            if isinstance(card, dict):
                card['subtitle'] = ((card.get('subtitle') + '. ') if card.get('subtitle') else '') + \
                    f"Couldn't read {', '.join(unreadable[:3])}; instances there aren't counted"
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
    """Accounts and instance counts for an environment, with rollout limits.

    Call before multi_account_execute, multi_account_rollback or multi_account_dry_run.

    Args:
        environment: dev, staging or prod.
        account_ids: Only when the operator names accounts.
        regions: Only when the operator names regions.
        ou_ids: Only when the operator names OUs.
    """
    try:
        env_value = _normalize_environment(environment)
        logger.info(f"[TOOL:resolve_execution_scope] env={env_value} accounts={account_ids} "
                     f"regions={regions} ou_ids={ou_ids}")

        scope = resolve_scope(environment=env_value, account_ids=account_ids,
                              regions=regions, ou_ids=ou_ids)
        if scope.get('error_code'):
            return scope  # an OutOfScope refusal names what was asked for and what's allowed

        fleet = _get_fleet_summary(environment=env_value)
        fleet_accounts = fleet.get('accounts', {})

        # Per-environment counts from the same instances a plan targets: the fleet view and
        # new ones EC2 reports. Never the account's totals across every environment when the
        # environment has none (2 Nov review, L7).
        env_instance_counts: Dict[str, int] = {}
        env_missing_counts: Dict[str, int] = {}
        if env_value:
            cache = _shared.fleet_instances()
            for iid, inst in _shared.environment_instances(env_value).items():
                acct = inst.get('account_id', '')
                env_instance_counts[acct] = env_instance_counts.get(acct, 0) + 1
                env_missing_counts[acct] = env_missing_counts.get(acct, 0) + (cache.get(iid) or {}).get('missing_count', 0)

        accounts = []
        total_instances = 0
        scope_account_ids = [s['account_id'] for s in scope['accounts']]
        total_missing = 0
        total_unmanaged = 0
        for acct_id in scope_account_ids:
            acct_data = fleet_accounts.get(acct_id, {})
            if env_value:
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
            # The environment's rollout policy; plans always use it, so it's shown, not passed.
            'rollout_policy': execution_limits(env_value),
            'next_action': ("Now call the write tool the operator asked for (multi_account_execute or "
                            "multi_account_rollback) with these account_ids. "
                            "It returns a plan card for the operator to approve, so don't stop to confirm first. "
                            "Call multi_account_dry_run instead only if they asked for a scan or preview."),
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"[TOOL:resolve_execution_scope] ERROR: {error_info}")
        return error_info
    except Exception as e:
        logger.error(f"[TOOL:resolve_execution_scope] ERROR: {e}")
        return classify_error(e)
