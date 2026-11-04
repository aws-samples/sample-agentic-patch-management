"""Maintenance window, Quick Setup patch policy association, health check, and verification tools."""

import re

from strands import tool
from typing import Optional, List, Dict, Tuple

from . import _shared
from ._shared import (
    logger, time,
    classify_error,
    ClientError,
    datetime, timezone,
    get_client, _ssm, _validate_instance_ids, _normalize_environment,
    _get_query_targets,
    _get_fleet_summary,
    _direct_describe_scoped_instances,
    _format_utc_as_local,
    _checked_reboot_option, run_limits, reboot_option as reboot_policy,
    SCOPE_TAG_KEY, SCOPE_TAG_VALUE,
    request_cached, forget_request_cache,
)

# Threads for reading maintenance windows per (account, region).
_WINDOW_WORKERS = 10


# ============================================================================
# LOCAL HELPERS (only used by tools in this module)
# ============================================================================

# -- Patch policy association cache (session-scoped) --
# list-associations is 1 API call regardless of fleet size.
# We cache both the raw list AND the enriched details to avoid repeated
# describe_association calls within the same agent session.
_patch_assoc_cache: Optional[List[dict]] = None
_patch_assoc_enriched_cache: Optional[List[dict]] = None
_patch_assoc_cache_time: float = 0
_PATCH_ASSOC_CACHE_TTL = 300  # 5 minutes


def _get_cached_patch_associations() -> List[dict]:
    """Get all patch-related SSM associations across hub + spoke accounts (cached 5 min).

    Fans out across all configured (account, region) pairs so spoke-account
    patch policies are discovered alongside hub policies.
    """
    global _patch_assoc_cache, _patch_assoc_cache_time
    now = time.time()
    if _patch_assoc_cache is not None and (now - _patch_assoc_cache_time) < _PATCH_ASSOC_CACHE_TTL:
        return _patch_assoc_cache

    patch_docs = {'AWS-RunPatchBaseline', 'AWS-RunPatchBaselineAssociation'}
    patch_assocs = []

    # Fan out across all configured accounts and regions
    for account_id, region in _get_query_targets():
        try:
            ssm_client = get_client('ssm', account_id=account_id, region=region)
            paginator = ssm_client.get_paginator('list_associations')
            for page in paginator.paginate():
                for assoc in page['Associations']:
                    if assoc.get('Name') in patch_docs:
                        assoc['_account_id'] = account_id
                        assoc['_region'] = region
                        patch_assocs.append(assoc)
        except Exception as e:
            logger.warning(f"[PATCH_POLICY] Could not list associations for {account_id}/{region}: {e}")

    _patch_assoc_cache = patch_assocs
    _patch_assoc_cache_time = now
    logger.info(f"[API:list_associations] cache MISS -- found {len(patch_assocs)} patch associations across {len(set(a['_account_id'] for a in patch_assocs))} account(s)")
    return patch_assocs


def _get_enriched_patch_associations() -> List[dict]:
    """Get patch associations with full details (cached alongside raw list).

    Calls describe_association for each patch association to get schedule,
    operation, and baseline override info. Cached for the same TTL as the
    raw association list.
    """
    global _patch_assoc_enriched_cache, _patch_assoc_cache_time
    now = time.time()
    if _patch_assoc_enriched_cache is not None and (now - _patch_assoc_cache_time) < _PATCH_ASSOC_CACHE_TTL:
        return _patch_assoc_enriched_cache

    patch_assocs = _get_cached_patch_associations()
    enriched = []
    for pa in patch_assocs:
        assoc_id = pa['AssociationId']
        # Use the correct account/region client (set during list_associations fan-out)
        acct = pa.get('_account_id', '')
        rgn = pa.get('_region', '')
        try:
            ssm_client = get_client('ssm', account_id=acct, region=rgn) if acct else _ssm()
            detail = ssm_client.describe_association(AssociationId=assoc_id)
            desc = detail['AssociationDescription']
            params = desc.get('Parameters', {})
            operation = 'Scan'
            for op_val in params.get('Operation', []):
                operation = op_val

            enriched.append({
                'association_id': assoc_id,
                'account_id': acct, 'region': rgn,
                'name': desc.get('AssociationName', pa.get('AssociationName', pa['Name'])),
                'document': pa['Name'],
                'operation': operation,
                'schedule': desc.get('ScheduleExpression', 'none'),
                'targets': pa.get('Targets', []),
                'status': pa.get('Overview', {}).get('Status', 'unknown'),
                'baseline_override': params.get('BaselineOverride', [None])[0],
            })
        except Exception as e:
            logger.warning(f"Could not describe association {assoc_id}: {e}")
            enriched.append({
                'association_id': assoc_id,
                'account_id': acct, 'region': rgn,
                'name': pa.get('AssociationName', pa['Name']),
                'document': pa['Name'],
                'operation': 'unknown',
                'schedule': 'unknown',
                'targets': pa.get('Targets', []),
                'status': 'unknown',
            })

    _patch_assoc_enriched_cache = enriched
    logger.info(f"Enriched {len(enriched)} patch associations")
    return enriched


def _match_association_to_instance(assoc_targets: List[dict], instance_id: str,
                                   instance_tags: Dict[str, str]) -> bool:
    """Check if an association's targets match a specific instance.

    SSM target semantics: multiple target entries are OR (any match = included).
    Within a single target entry, key/values define the filter.

    Handles all SSM target types:
    - InstanceIds: ['*'] -> matches all
    - InstanceIds: ['i-abc', 'i-def'] -> explicit list
    - tag:Key: ['Value1', 'Value2'] -> tag-based targeting (value in list)
    - tag-key: ['KeyName'] -> instances that have the tag (any value)
    """
    if not assoc_targets:
        return False

    # OR logic: if ANY target entry matches, the instance is included
    for target in assoc_targets:
        key = target.get('Key', '')
        values = target.get('Values', [])

        if key == 'InstanceIds':
            if values == ['*'] or instance_id in values:
                return True

        elif key.startswith('tag:'):
            tag_name = key[4:]  # strip 'tag:' prefix
            tag_value = instance_tags.get(tag_name)
            if tag_value in values:
                return True

        elif key == 'tag-key':
            if any(v in instance_tags for v in values):
                return True

    return False


def _match_window_target(filters: List[dict], instance_id: str, instance_tags: Dict[str, str]) -> bool:
    """Whether one maintenance window target covers an instance.

    SSM requires an instance to match every filter in a window target (they
    are ANDed); separate targets registered on a window are either-or.
    Resource-group targets can't be resolved here, so they don't count.
    """
    if not filters:
        return False
    for f in filters:
        key, values = f.get('Key', ''), f.get('Values', [])
        if key == 'InstanceIds':
            ok = values == ['*'] or instance_id in values
        elif key.startswith('tag:'):
            ok = instance_tags.get(key[4:]) in values
        elif key == 'tag-key':
            ok = any(v in instance_tags for v in values)
        else:
            ok = False
        if not ok:
            return False
    return True


def _patch_task(ssm_client, window_id: str, task: dict) -> Optional[str]:
    """The reboot option of a window task that runs AWS-RunPatchBaseline with Operation=Install
    (the document defaults to Scan, and to RebootIfNeeded), or None when it doesn't install."""
    if task.get('TaskArn') != 'AWS-RunPatchBaseline':
        return None
    params = {k: (v or {}).get('Values') for k, v in (task.get('TaskParameters') or {}).items()}
    if params.get('Operation') is None:
        detail = ssm_client.get_maintenance_window_task(WindowId=window_id, WindowTaskId=task.get('WindowTaskId'))
        params = ((detail.get('TaskInvocationParameters') or {}).get('RunCommand') or {}).get('Parameters') \
            or {k: (v or {}).get('Values') for k, v in (detail.get('TaskParameters') or {}).items()}
    if 'Install' not in (params.get('Operation') or []):
        return None
    return (params.get('RebootOption') or ['RebootIfNeeded'])[0]




def _paged(ssm_client, operation: str, key: str, **kwargs) -> List[dict]:
    out: List[dict] = []
    for page in ssm_client.get_paginator(operation).paginate(**kwargs):
        out.extend(page.get(key, []))
    return out


def _get_maintenance_windows_for_target(account_id: str, region: str,
                                        env_value: Optional[str],
                                        instance_ids: Optional[list],
                                        instance_tags: Dict[str, Dict[str, str]]) -> tuple:
    """Per-(account, region) maintenance window lookup: (enriched window dicts, unreadable).

    unreadable lists what couldn't be read ({account_id, region, window_id?, error}): a
    window that can't be read isn't the same as no window (2 Nov review, M2).
    """
    from helper.retry import throttled
    unreadable: List[dict] = []

    def failed(e, window_id=None):
        entry = {'account_id': account_id, 'region': region, 'error': str(e)[:200]}
        if window_id:
            entry['window_id'] = window_id
        unreadable.append(entry)

    try:
        ssm_client = get_client('ssm', account_id=account_id, region=region)
    except Exception as e:
        logger.warning(f"[TOOL:get_maintenance_windows] SSM client failed for {account_id}/{region}: {e}")
        failed(e)
        return [], unreadable

    def cached(*key, fetch):
        return request_cached(('mw', account_id, region) + key, fetch)

    try:
        window_identities = cached('windows', fetch=lambda: throttled(lambda: _paged(
            ssm_client, 'describe_maintenance_windows', 'WindowIdentities')))
    except Exception as e:
        logger.warning(f"[TOOL:get_maintenance_windows] describe_maintenance_windows failed for {account_id}/{region}: {e}")
        failed(e)
        return [], unreadable

    windows: List[dict] = []
    for window_identity in window_identities:
        window_id = window_identity['WindowId']
        try:
            # Targets first: they name the environment, so other environments' windows
            # cost one call each, not four.
            window_targets = cached(window_id, 'targets', fetch=lambda: throttled(lambda: _paged(
                ssm_client, 'describe_maintenance_window_targets', 'Targets', WindowId=window_id)))
            env_values = [v for target in window_targets for f in target.get('Targets', [])
                          if f.get('Key') == 'tag:Environment' for v in f.get('Values') or []]
            window_environment = env_values[0] if env_values else 'unknown'
            # Asked about particular instances, a window counts if its targets match them,
            # however it targets them (Patch Group tag, instance IDs); otherwise it's listed
            # under every environment it names (2 Nov review, L4).
            matches_instances = bool(instance_ids and instance_tags) and any(
                _match_window_target(t.get('Targets', []), iid, instance_tags.get(iid, {}))
                for t in window_targets for iid in instance_ids)
            if (env_value and not matches_instances
                    and env_value not in {_normalize_environment(v) for v in env_values}):
                continue

            window_details = cached(window_id, 'details', fetch=lambda: ssm_client.get_maintenance_window(WindowId=window_id))
            # Reboot options of the window's install tasks; a reboot from any of them counts.
            reboots = cached(window_id, 'installs', fetch=lambda: [
                r for r in (_patch_task(ssm_client, window_id, t) for t in _paged(
                    ssm_client, 'describe_maintenance_window_tasks', 'Tasks', WindowId=window_id)) if r])
            has_patch_task = bool(reboots)

            next_execution = window_details.get('NextExecutionTime')
            if next_execution:
                next_execution = next_execution.isoformat() if hasattr(next_execution, 'isoformat') else str(next_execution)

            hours_until_window = None
            if next_execution:
                try:
                    next_str = str(next_execution)
                    if '+' in next_str or next_str.endswith('Z'):
                        next_str = next_str.replace('Z', '+00:00')
                    next_dt = datetime.fromisoformat(next_str)
                    if next_dt.tzinfo is None:
                        next_dt = next_dt.replace(tzinfo=timezone.utc)
                    delta = next_dt - datetime.now(timezone.utc)
                    hours_until_window = max(0, delta.total_seconds() / 3600)
                except Exception:
                    pass

            target_list: list = []
            instances_covered: list = []

            for target in window_targets:
                target_filters = target.get('Targets', [])
                target_list.append({
                    'resource_type': target.get('ResourceType'),
                    'filters': target_filters,
                })
                if instance_ids and instance_tags:
                    for iid in instance_ids:
                        if iid not in instances_covered and \
                                _match_window_target(target_filters, iid, instance_tags.get(iid, {})):
                            instances_covered.append(iid)

            window_info = {
                'window_id': window_id,
                'account_id': account_id,
                'region': region,
                'name': window_details.get('Name'),
                'schedule': window_details.get('Schedule', ''),
                'next_execution': _format_utc_as_local(next_execution) if next_execution else None,
                'next_execution_utc': str(next_execution) if next_execution else None,
                'hours_until_window': round(hours_until_window, 1) if hours_until_window is not None else None,
                'duration_hours': window_details.get('Duration'),
                'cutoff_hours': window_details.get('Cutoff'),
                'enabled': window_details.get('Enabled'),
                'has_patch_task': has_patch_task,
                'reboot_option': ('RebootIfNeeded' if 'RebootIfNeeded' in reboots else reboots[0]) if reboots else None,
                'target_count': len(target_list),
                'environment': window_environment,
            }

            if instance_ids:
                not_covered = [iid for iid in instance_ids if iid not in instances_covered]
                window_info['instances_covered'] = instances_covered
                window_info['instances_not_covered'] = not_covered
                window_info['coverage_percentage'] = int((len(instances_covered) / len(instance_ids)) * 100) if instance_ids else 0

            windows.append(window_info)
        except Exception as window_error:
            logger.warning(f"Error processing window {window_id} in {account_id}/{region}: {window_error}")
            failed(window_error, window_id)
            continue

    return windows, unreadable


# ============================================================================
# TOOLS
# ============================================================================


@tool
def get_maintenance_windows(environment: str, instance_ids: list = None) -> dict:
    """Maintenance windows (when patching may run) for an environment.

    Use for questions about windows, scheduling or the next patch window.

    Args:
        environment: dev, staging or prod.
        instance_ids: Check these instances are covered.
    """
    # Windows are regional SSM resources, so this fans out over every (account, region)
    # target and records account_id and region on each window.
    try:
        env_value = _normalize_environment(environment)
        targets = _get_query_targets()
        logger.info(f"[TOOL:get_maintenance_windows] environment={environment} "
                    f"instance_ids={len(instance_ids) if instance_ids else 0} "
                    f"targets={len(targets)}")

        # Tag lookup is global across instances. Try the fleet cache first, then
        # fall back to per-target EC2 lookups for any IDs we can't locate.
        instance_tags: Dict[str, Dict[str, str]] = {}
        if instance_ids:
            try:
                _get_fleet_summary()
            except Exception:
                pass
            # Real tags, since window targets can filter on any tag; the fleet
            # cache only knows Environment and the scope tag.
            try:
                for iid, info in _shared._direct_describe_scoped_instances(instance_ids=instance_ids).items():
                    instance_tags[iid] = info.get('tags') or {}
            except Exception as e:
                logger.warning(f"[TOOL:get_maintenance_windows] tag lookup failed: {e}")
            cache = _shared.fleet_instances()
            unresolved = [iid for iid in instance_ids if iid not in cache and iid not in instance_tags]
            for iid, info in cache.items():
                if iid in instance_ids and iid not in instance_tags:
                    instance_tags[iid] = {
                        'Environment': info.get('environment', ''),
                        SCOPE_TAG_KEY: SCOPE_TAG_VALUE if info.get('managed') else '',
                    }
            # For unresolved IDs, ask each (account, region) target until we find them
            for (acct, rgn) in targets:
                if not unresolved:
                    break
                try:
                    ec2 = get_client('ec2', account_id=acct, region=rgn)
                    resp = ec2.describe_instances(InstanceIds=unresolved)
                    for reservation in resp['Reservations']:
                        for instance in reservation['Instances']:
                            iid = instance['InstanceId']
                            tags = {tag['Key']: tag['Value'] for tag in instance.get('Tags', [])}
                            instance_tags[iid] = tags
                    unresolved = [iid for iid in unresolved if iid not in instance_tags]
                except Exception:
                    continue

        all_windows: List[dict] = []
        unreadable: List[dict] = []

        from concurrent.futures import as_completed as _ac
        from helper.concurrency import ContextThreadPoolExecutor as _TPE
        with _TPE(max_workers=min(len(targets) or 1, _WINDOW_WORKERS)) as executor:
            futures = {
                executor.submit(_get_maintenance_windows_for_target, a, r, env_value, instance_ids, instance_tags): (a, r)
                for (a, r) in targets
            }
            for fut in _ac(futures):
                acct, rgn = futures[fut]
                try:
                    found, unread = fut.result()
                    all_windows.extend(found)
                    unreadable.extend(unread)
                except Exception as e:
                    logger.warning(f"[TOOL:get_maintenance_windows] {acct}/{rgn} failed: {e}")
                    unreadable.append({'account_id': acct, 'region': rgn, 'error': str(e)[:200]})

        return {
            'windows': all_windows,
            'count': len(all_windows),
            'environment_filter': environment if environment else 'all',
            'instances_validated': len(instance_ids) if instance_ids else 0,
            'queried_targets': [{'account_id': a, 'region': r} for (a, r) in targets],
            # Windows here may be missing: anything deciding "no window" must say so.
            'unreadable': unreadable,
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"Error getting maintenance windows: {error_info}")
        error_info["windows"] = []
        error_info["count"] = 0
        return error_info
    except Exception as e:
        logger.error(f"Error getting maintenance windows: {e}")
        result = classify_error(e)
        result["windows"] = []
        result["count"] = 0
        return result


_WINDOW_NAME_RE = re.compile(r'^[A-Za-z0-9_.-]{3,128}$')


def _create_maintenance_window_impl(name: str, schedule: str, duration: int,
                                    target_environment: str, confirm_execute: bool = False,
                                    reboot_option: Optional[str] = None) -> dict:
    """Validate a maintenance window request and, when confirmed, create it with a patch task.

    The window installs approved patches (AWS-RunPatchBaseline, Install) on the
    environment's scoped instances in this account and Region, within the
    environment's rollout limits and the plan's reboot choice.

    Returns a confirmation_required preview unless confirm_execute is True.
    """
    problems = []
    if not isinstance(name, str) or not _WINDOW_NAME_RE.match(name):
        problems.append("name must be 3-128 letters, digits, '.', '-' or '_'")
    if not isinstance(schedule, str) or not re.match(r'^(cron|at)\(.+\)$', schedule.strip()):
        problems.append("schedule must be a cron(...) or at(...) expression")
    if not isinstance(duration, int) or not 1 <= duration <= 24:
        problems.append("duration must be 1-24 hours")
    if problems:
        return {"error": "; ".join(problems), "error_code": "InvalidParameter", "category": "ABORT",
                "retryable": False, "result_type": "error"}
    env_value = _normalize_environment(target_environment)
    if not confirm_execute:
        return {
            "status": "confirmation_required",
            "warning": f"About to create maintenance window '{name}' for {env_value}.",
            "execution_plan": {"name": name, "schedule": schedule, "duration": duration,
                               "environment": env_value},
        }
    try:
        # Tag-on-create: the IAM policy in core-stack.ts requires
        # ManagedBy=IntelligentPatchAutomation on creation. The same tag is
        # used by the Modify policy to gate update/delete operations on this
        # window. Without the tag, the agent cannot manage the window it just
        # created (closed-loop tag-based scoping).
        forget_request_cache()  # this turn's later window reads must see the new window
        response = _ssm().create_maintenance_window(
            Name=name,
            Schedule=schedule,
            Duration=duration,
            Cutoff=1,
            AllowUnassociatedTargets=False,
            Tags=[
                {'Key': 'ManagedBy', 'Value': 'IntelligentPatchAutomation'},
            ],
        )

        window_id = response['WindowId']

        # Register targets -- include both Environment AND scope tag so the window
        # only patches instances explicitly in scope (PatchAutomation=enabled).
        env_value = _normalize_environment(target_environment)
        target = _ssm().register_target_with_maintenance_window(
            WindowId=window_id,
            ResourceType='INSTANCE',
            Targets=[
                {
                    'Key': 'tag:Environment',
                    'Values': [env_value]
                },
                {
                    'Key': f'tag:{SCOPE_TAG_KEY}',
                    'Values': [SCOPE_TAG_VALUE]
                },
            ]
        )

        # The patch task: without it the window opens and does nothing, and
        # Patchy's own SLA checks (which look for Install tasks) ignore it.
        limits = run_limits(env_value)
        try:
            task = _ssm().register_task_with_maintenance_window(
                WindowId=window_id,
                Name=f"{name}-install"[:128],
                TaskArn='AWS-RunPatchBaseline',
                TaskType='RUN_COMMAND',
                Targets=[{'Key': 'WindowTargetIds', 'Values': [target['WindowTargetId']]}],
                Priority=1,
                MaxConcurrency=limits['send_command_max_concurrency'],
                MaxErrors=limits['send_command_max_errors'],
                TaskInvocationParameters={'RunCommand': {'Parameters': {
                    'Operation': ['Install'],
                    'RebootOption': [_checked_reboot_option(reboot_option or reboot_policy(env_value))],
                }}},
            )
        except Exception:
            # A window that can't patch is removed rather than left looking scheduled.
            try:
                _ssm().delete_maintenance_window(WindowId=window_id)
            except Exception as cleanup:
                logger.error(f"Could not remove window {window_id} after its task failed: {cleanup}")
            raise

        return {
            'window_id': window_id,
            'window_task_id': task['WindowTaskId'],
            'name': name,
            'schedule': schedule
        }
    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"Error creating maintenance window: {error_info}")
        return error_info
    except Exception as e:
        logger.error(f"Error creating maintenance window: {e}")
        return classify_error(e)


@tool
def get_patch_policy(instance_ids: List[str], environment: Optional[str] = None) -> dict:
    """Quick Setup patch policies (what gets patched, on what schedule) for instances.

    Use when the operator asks about patch policies or recurring schedules, or when planning
    a scheduled rollout alongside get_maintenance_windows.

    Args:
        instance_ids: Instances to check.
        environment: dev, staging or prod.
    """
    # One cached list-associations call plus client-side tag matching, so it scales
    # without per-instance API calls.
    try:
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error

        logger.info(f"[TOOL:get_patch_policy] instances={len(instance_ids)} environment={environment}")

        # Get instance tags (we need these for target matching).
        # Use _direct_describe_scoped_instances which fans out across all
        # configured (account, region) pairs -- handles spoke instances.
        instance_tags_map: Dict[str, Dict[str, str]] = {}
        instance_location: Dict[str, tuple] = {}
        try:
            described = _direct_describe_scoped_instances(instance_ids=instance_ids)
            for iid, info in described.items():
                instance_tags_map[iid] = info.get('tags', {})
                instance_location[iid] = (info.get('account_id'), info.get('region'))
        except Exception as e:
            logger.warning(f"Could not get instance tags via cross-account describe: {e}")

        # Get cached + enriched patch associations (cached 5 min)
        enriched_assocs = _get_enriched_patch_associations()

        # Match associations to instances
        instance_policies: Dict[str, List[dict]] = {iid: [] for iid in instance_ids}
        for assoc in enriched_assocs:
            for iid in instance_ids:
                tags = instance_tags_map.get(iid, {})
                # An association runs only in its own account and Region (2 Nov review, L9).
                where = instance_location.get(iid)
                if where and assoc.get('account_id') and (assoc['account_id'], assoc.get('region')) != where:
                    continue
                if _match_association_to_instance(assoc['targets'], iid, tags):
                    instance_policies[iid].append(assoc)

        # Classify instances
        without_policy = []
        with_install = []
        with_scan_only = []
        for iid, policies in instance_policies.items():
            install_policies = [p for p in policies if p['operation'] == 'Install']
            scan_policies = [p for p in policies if p['operation'] == 'Scan']
            if install_policies:
                with_install.append(iid)
            elif scan_policies:
                with_scan_only.append(iid)
            else:
                without_policy.append(iid)

        total = len(instance_ids)
        summary_parts = []
        if with_install:
            summary_parts.append(f"{len(with_install)} have Quick Setup Install association")
        if with_scan_only:
            summary_parts.append(f"{len(with_scan_only)} have Quick Setup Scan-only association")
        if without_policy:
            summary_parts.append(f"{len(without_policy)} have NO Quick Setup patch policy association")
        summary = f"{total} instances: {', '.join(summary_parts)}"

        return {
            'instance_policies': instance_policies,
            'instances_without_policy': without_policy,
            'instances_with_install_policy': with_install,
            'instances_with_scan_only': with_scan_only,
            'total_associations_checked': len(enriched_assocs),
            'summary': summary,
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"Error checking patch policies: {error_info}")
        return error_info
    except Exception as e:
        logger.error(f"Error checking patch policies: {e}")
        return classify_error(e)


@tool
def check_instance_health(instance_ids: List[str]) -> dict:
    """SSM agent health for instances.

    Args:
        instance_ids: Instances to check.
    """
    try:
        # Validate instance IDs
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error

        # Group instances by (account, region) for cross-region support
        by_location = _shared._locate_instances(instance_ids)

        # Query SSM per (account, region) -- describe_instance_information is regional
        from helper.retry import throttled
        instance_info_map: Dict[str, dict] = {}
        unread: Dict[str, str] = {}
        for (account_id, region), iids in by_location.items():
            try:
                ssm_client = get_client('ssm', account_id=account_id, region=region)
                for n in range(0, len(iids), 50):  # the InstanceIds filter takes at most 50 values
                    pages = throttled(lambda: list(ssm_client.get_paginator('describe_instance_information').paginate(
                        Filters=[{'Key': 'InstanceIds', 'Values': iids[n:n + 50]}])))
                    for page in pages:
                        for info in page.get('InstanceInformationList', []):
                            instance_info_map[info['InstanceId']] = info
            except Exception as e:
                # Not the agent's fault: SSM couldn't be read there. Those instances are
                # unknown, not unreachable, and mustn't count towards a rollback (2 Nov review, H5).
                logger.warning(f"[TOOL:check_instance_health] SSM query failed for {account_id}/{region}: {e}")
                for iid in iids:
                    unread.setdefault(iid, f"{account_id}/{region}")

        health_results = []
        for instance_id in instance_ids:
            instance_info = instance_info_map.get(instance_id)

            if not instance_info and instance_id in unread:
                health_results.append({
                    'instance_id': instance_id,
                    'ssm_status': 'UNKNOWN',
                    'health_score': None,
                    'issues': [f"Systems Manager couldn't be read in {unread[instance_id]}"]
                })
                continue
            if not instance_info:
                health_results.append({
                    'instance_id': instance_id,
                    'ssm_status': 'UNREACHABLE',
                    'health_score': 0,
                    'issues': ['SSM agent not responding']
                })
                continue

            ping_status = instance_info.get('PingStatus', 'Unknown')
            health_score = 100 if ping_status == 'Online' else 0
            issues = [] if ping_status == 'Online' else [f'SSM ping status: {ping_status}']

            health_results.append({
                'instance_id': instance_id,
                'ssm_status': ping_status,
                'health_score': health_score,
                'issues': issues,
                'last_ping': str(instance_info.get('LastPingDateTime', 'N/A'))
            })

        # Calculate overall health, over the instances whose state was read
        scored = [r for r in health_results if r['health_score'] is not None]
        avg_health = sum(r['health_score'] for r in scored) / len(scored) if scored else 0
        unhealthy_count = sum(1 for r in scored if r['health_score'] < 100)
        unknown_count = len(health_results) - len(scored)

        logger.info(f"[TOOL:check_instance_health] RESULT: instances={len(instance_ids)} avg_health={avg_health:.0f} unhealthy={unhealthy_count}")

        return {
            'results': health_results,
            'overall_health': avg_health,
            'total_instances': len(instance_ids),
            'unhealthy_instances': unhealthy_count,
            'unknown_instances': unknown_count,
            'unreadable_locations': sorted(set(unread.values())),
            # Healthy means every instance; one unreachable instance is worth a look before promoting.
            # Health that couldn't be read is UNKNOWN (hold), never a reason to roll back.
            'recommendation': ('ROLLBACK_RECOMMENDED' if scored and avg_health < 50
                               else 'UNKNOWN' if unknown_count
                               else 'HEALTHY' if health_results and unhealthy_count == 0
                               else 'INVESTIGATE'),
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"Error checking instance health: {error_info}")
        error_info["instance_ids"] = instance_ids
        return error_info
    except Exception as e:
        logger.error(f"Error checking instance health: {e}")
        return classify_error(e)


def _evaluation_seconds(alarm: dict) -> int:
    """How long an alarm takes to go into ALARM after its metric breaches: Period x EvaluationPeriods.
    A metric math alarm has its periods on its metrics."""
    periods = [alarm.get('Period') or 0] + [
        (m.get('MetricStat') or {}).get('Period') or m.get('Period') or 0 for m in alarm.get('Metrics') or []]
    return max(periods) * max(alarm.get('EvaluationPeriods') or 1, alarm.get('DatapointsToAlarm') or 1)


def _iso(ts) -> str:
    return ts.isoformat() if isinstance(ts, datetime) else str(ts or 'N/A')


@tool
def check_cloudwatch_alarms(instance_ids: List[str], alarm_name_pattern: Optional[str] = None) -> dict:
    """CloudWatch alarm state for instances.

    Args:
        instance_ids: Instances to check.
        alarm_name_pattern: Only alarms whose names contain this, e.g. httpd.
    """
    try:
        # Validate instance IDs
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error

        # Group instances by (account, region) for cross-region support
        by_location = _shared._locate_instances(instance_ids)

        # Fetch alarms per (account, region) -- CloudWatch is regional. A location
        # that can't be read, or an instance that can't be placed, is unknown, not
        # healthy, so a failed read can't let a rollout be promoted. Every state is
        # read: how long the instances' alarms take to evaluate, and which have no
        # data, matter to verify_and_proceed (2 Nov review, T4).
        from helper.retry import throttled
        metric_alarms: list = []
        unreadable: List[str] = []
        for (account_id, region), iids in by_location.items():
            try:
                cw_client = get_client('cloudwatch', account_id=account_id, region=region)
                paginator = cw_client.get_paginator('describe_alarms')
                pages = throttled(lambda: list(paginator.paginate(MaxRecords=100)))
                for page in pages:
                    metric_alarms.extend(page.get('MetricAlarms', []))
            except Exception as e:
                logger.warning(f"[TOOL:check_cloudwatch_alarms] CW query failed for {account_id}/{region}: {e}")
                unreadable.append(f"{account_id}/{region}")
        located = {i for iids in by_location.values() for i in iids}
        unlocated = [i for i in instance_ids if i not in located]

        alarm_results = []
        evaluation_seconds = 0
        for instance_id in instance_ids:
            instance_alarms = []
            no_data = []
            for alarm in metric_alarms:
                dimensions = alarm.get('Dimensions', [])
                has_instance = any(
                    d['Name'] == 'InstanceId' and d['Value'] == instance_id
                    for d in dimensions
                )
                if not has_instance:
                    continue
                if alarm_name_pattern is not None and alarm_name_pattern.lower() not in alarm['AlarmName'].lower():
                    continue
                evaluation_seconds = max(evaluation_seconds, _evaluation_seconds(alarm))
                entry = {
                    'name': alarm['AlarmName'],
                    'state': alarm['StateValue'],
                    'reason': alarm.get('StateReason', 'Unknown'),
                    'timestamp': _iso(alarm.get('StateUpdatedTimestamp'))
                }
                if alarm['StateValue'] == 'ALARM':
                    instance_alarms.append(entry)
                elif alarm['StateValue'] == 'INSUFFICIENT_DATA':
                    no_data.append(entry)

            # Limit to 5 alarms to avoid token bloat
            instance_alarms = instance_alarms[:5]

            alarm_results.append({
                'instance_id': instance_id,
                'alarm_count': len(instance_alarms),
                'alarms': instance_alarms,
                **({'insufficient_data': no_data[:5]} if no_data else {}),
                'is_healthy': len(instance_alarms) == 0
            })

        unhealthy_count = sum(1 for r in alarm_results if not r['is_healthy'])
        total_alarms = sum(r['alarm_count'] for r in alarm_results)

        logger.info(f"[TOOL:check_cloudwatch_alarms] RESULT: instances={len(instance_ids)} unhealthy={unhealthy_count} alarms_firing={total_alarms}")

        result = {
            'results': alarm_results,
            'total_instances': len(instance_ids),
            'unhealthy_instances': unhealthy_count,
            'total_alarms_firing': total_alarms,
            'recommendation': ('INVESTIGATE' if unhealthy_count else
                               'UNKNOWN' if unreadable or unlocated else 'HEALTHY'),
            'unreadable_locations': unreadable,
            'unlocated_instances': unlocated,
            # The longest an instance alarm takes to evaluate (Period x EvaluationPeriods).
            'longest_evaluation_seconds': evaluation_seconds,
            'message': (
                f'{unhealthy_count} instances have alarms firing ({total_alarms} total alarms). '
                'Check alarm timestamps -- pre-existing alarms may not be related to the current operation.'
                if unhealthy_count > 0
                else (f"No alarms firing on the instances read; alarms for {len(unlocated)} instance(s) and "
                      f"{len(unreadable)} location(s) couldn't be read" if unreadable or unlocated
                      else 'All instances healthy - no alarms firing')
            )
        }

        return result

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"Error checking CloudWatch alarms: {error_info}", exc_info=True)
        error_info["instance_ids"] = instance_ids
        error_info["recommendation"] = "UNKNOWN"
        return error_info
    except Exception as e:
        logger.error(f"Error checking CloudWatch alarms: {e}", exc_info=True)
        result = classify_error(e)
        result["instance_ids"] = instance_ids
        result["recommendation"] = "UNKNOWN"
        return result


_PATCH_PLAN_TOOLS = ("execute_patch_operation", "multi_account_execute")


def _run_plan(execution_id: str) -> Optional[dict]:
    """The approved patch plan behind a run, or None.

    Any conversation may verify a run (patch dev today, verify tomorrow in a
    new chat): what gets recorded comes from the stored plan, not the caller.
    """
    from helper import plans
    rec = plans.get_record(f"run-executions/{execution_id}.json")
    if not rec:
        return None
    plan = plans.load_plan(rec["plan_id"])
    return plan if plan.get("tool") in _PATCH_PLAN_TOOLS else None


def _plan_siblings(execution_id: str) -> Optional[List[str]]:
    """The other executions the same approved plan started (plan-executions/), if any;
    None when the records couldn't be read, which is not the same as "none"."""
    from helper import plans
    try:
        rec = plans.get_record(f"run-executions/{execution_id}.json")
        group = plans.get_record(f"plan-executions/{rec['plan_id']}.json") if rec else None
    except Exception as e:
        logger.warning(f"[TOOL:verify_and_proceed] plan executions not readable for {execution_id}: {e}")
        return None
    return [e for e in (group or {}).get("execution_ids") or [] if e != execution_id]


def _run_instances(plan: dict) -> Tuple[List[str], List[str]]:
    """The instances a patch plan targeted, and the locations that couldn't be listed.

    Named instances, or the environment's scoped instances now; an environment
    listing that couldn't read some locations leaves their instances out.
    """
    params = plan.get("params") or {}
    if params.get("instance_ids"):
        return list(params["instance_ids"]), []
    found = _shared.environment_instances(_normalize_environment(params.get("environment") or ""),
                                          params.get("account_ids") or None)
    return list(found), list(getattr(found, "unreadable", None) or [])


def _record_verified_stage(execution_id: str, environment: str, ended_at: Optional[str] = None) -> None:
    """Record that the plan behind this run was verified in its environment (staged promotion).

    ended_at is when the plan's last location finished; the record ages from it,
    not from when the run was verified (2 Nov review, T8).

    The environment and scope come from the approved plan, not the caller: only
    a patch run from an approved plan counts, the environment argument must
    match the plan's, and in JWT mode the caller must be an operator. Anything
    else records nothing.
    """
    from helper import caller, change_controls
    try:
        if not caller.may_change():
            logger.warning(f"[PROMOTION] not recorded for {execution_id}: caller is not an operator")
            return
        plan = _run_plan(execution_id)
        if not plan:
            return
        planned = _normalize_environment((plan.get("params") or {}).get("environment") or "")
        if planned != _normalize_environment(environment or ""):
            logger.warning(f"[PROMOTION] not recorded for {execution_id}: run was in {planned}, not {environment}")
            return
        change_controls.record_promotion(planned, change_controls.scope_key(plan.get("params") or {}), execution_id,
                                         ended_at=ended_at)
    except Exception as e:
        logger.warning(f"[PROMOTION] not recorded for {execution_id}: {e}")


# CloudWatch needs the instances' alarms' evaluation periods after a run before alarms mean
# anything: at least ALARM_SETTLE_SECONDS, otherwise the longest Period x EvaluationPeriods
# plus a margin, up to ALARM_SETTLE_MAX_SECONDS (2 Nov review, T4).
ALARM_SETTLE_SECONDS = 120
ALARM_SETTLE_MARGIN_SECONDS = 60
ALARM_SETTLE_MAX_SECONDS = 1800


def _settle_seconds(alarm_health: dict) -> int:
    longest = alarm_health.get('longest_evaluation_seconds') or 0
    return min(max(ALARM_SETTLE_SECONDS, longest + ALARM_SETTLE_MARGIN_SECONDS if longest else 0),
               ALARM_SETTLE_MAX_SECONDS)


def _no_data_since(alarm_health: dict, started_at: Optional[str]) -> List[str]:
    """Instance alarms with no data that changed state after the run started (or, when the
    start isn't known, any): the metric may have stopped because of the run."""
    start = datetime.fromisoformat(started_at) if started_at else None
    names = []
    for r in alarm_health.get('results') or []:
        for a in r.get('insufficient_data') or []:
            try:
                changed = datetime.fromisoformat(a['timestamp'])
            except (KeyError, TypeError, ValueError):
                changed = None
            if start is None or changed is None or changed >= start:
                names.append(f"{a.get('name')} on {r.get('instance_id')}")
    return names


@tool
def verify_and_proceed(automation_execution_id: str, instance_ids: List[str],
                       environment: str, alarm_pattern: Optional[str] = None) -> dict:
    """Check a patch run finished and the instances are healthy before promoting to the next environment.

    Use when the operator asks to verify a run, or before moving from one environment to the
    next. Returns WAIT at once while the run is going or alarms are still settling.

    Args:
        automation_execution_id: Execution ID from the patch run card.
        instance_ids: Instances that were patched.
        environment: Environment that was patched.
        alarm_pattern: Only check alarms whose names contain this.
    """
    # Non-blocking: one status read, then health and alarms once the run has ended
    # and alarms have had ALARM_SETTLE_SECONDS to evaluate.
    try:
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error

        from .patch_tools import _run_outcome, _run_summary
        state, run = _run_outcome(automation_execution_id)
        if state == 'ERROR':
            run['recommendation'] = 'INVESTIGATE'
            return run
        if state == 'RUNNING':
            return {'status': 'IN_PROGRESS', 'recommendation': 'WAIT', 'run': _run_summary(run),
                    'reason': f"Patch run is {run['status']}. Check again in a few minutes."}
        if run.get('status') in ('Cancelled', 'Cancelling'):
            # A stop is the operator's call, not a failure: they decide whether to roll back.
            return {'status': 'STOPPED', 'recommendation': 'HOLD', 'run': _run_summary(run),
                    'reason': "An operator stopped this run, so it isn't verified. Instances already being "
                              "patched may have finished: the run's counts are per location, so a stopped "
                              "location shows no successes even then; its report counts the instances. Check "
                              "the fix or health per instance, and roll back only if that's what you want."}
        if state == 'FAILED':
            return {'status': 'FAILED', 'recommendation': 'ROLLBACK', 'run': _run_summary(run),
                    'reason': f"Patch run ended {run['status']}."}
        if state == 'PARTIAL':
            return {'status': 'PARTIAL', 'recommendation': 'HOLD', 'run': _run_summary(run),
                    'reason': f"{run.get('children_failed') or 0} of {run.get('children_total')} account/region "
                              f"runs failed and {run.get('children_cancelled') or 0} were stopped. "
                              "Review them before promoting."}

        # A plan by instance ID starts one run per account and Region; the plan
        # is verified only when every one of them has succeeded.
        siblings = _plan_siblings(automation_execution_id)
        if siblings is None:
            # Another location may have failed; promoting on this one alone could hide it (2 Nov review, H2).
            return {'status': 'UNKNOWN', 'recommendation': 'HOLD', 'run': _run_summary(run),
                    'reason': "The plan's other locations couldn't be read, so Patchy can't confirm they all "
                              "succeeded. Check again in a few minutes."}
        ended_times = [run.get('ended_at')]
        started_times = [run.get('started_at')]
        for sibling in siblings:
            s_state, s_run = _run_outcome(sibling)
            ended_times.append(s_run.get('ended_at'))
            started_times.append(s_run.get('started_at'))
            if s_state == 'RUNNING':
                return {'status': 'IN_PROGRESS', 'recommendation': 'WAIT', 'run': _run_summary(s_run),
                        'reason': f"Another location of this plan ({sibling}) is still running."}
            if s_state != 'SUCCEEDED':
                return {'status': 'PARTIAL', 'recommendation': 'HOLD', 'run': _run_summary(s_run),
                        'reason': f"Another location of this plan ({sibling}) ended {s_run.get('status')}. "
                                  "Review it before promoting."}

        # Health is checked on the instances the approved plan targeted, when
        # there is one, not on whatever list the request names.
        try:
            plan = _run_plan(automation_execution_id)
        except Exception as e:
            logger.warning(f"[TOOL:verify_and_proceed] run plan not readable: {e}")
            plan = None
        unlisted: List[str] = []
        if plan:
            planned, unlisted = _run_instances(plan)
            instance_ids = planned or instance_ids

        # Alarms settle from when the last location finished, not just this one (2 Nov review, T3).
        ended_at = max((t for t in ended_times if t), default=None)
        started_at = min(started_times) if all(started_times) else None
        elapsed = None
        if ended_at:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(ended_at)).total_seconds()
            wait = int(ALARM_SETTLE_SECONDS - elapsed)
            if wait > 0:
                return {'status': 'SETTLING', 'recommendation': 'WAIT', 'retry_after_seconds': wait,
                        'reason': f"Run finished {int(elapsed)}s ago; alarms need at least "
                                  f"{ALARM_SETTLE_SECONDS // 60} minutes to evaluate. Check again in {wait}s."}

        # Run health checks in parallel (independent API calls)
        from helper.concurrency import ContextThreadPoolExecutor as ThreadPoolExecutor
        # Not a with block: its exit would wait on a hung check and make the timeouts below meaningless.
        executor = ThreadPoolExecutor(max_workers=2)
        try:
            future_ssm = executor.submit(check_instance_health, instance_ids)
            future_alarms = executor.submit(check_cloudwatch_alarms, instance_ids, alarm_pattern)
            try:
                ssm_health = future_ssm.result(timeout=15)
            except TimeoutError:
                logger.warning("[TOOL:verify_and_proceed] Worker timed out for SSM health check")
                ssm_health = {'recommendation': 'UNKNOWN', 'error': 'timeout'}
            except Exception as e:
                logger.warning(f"[TOOL:verify_and_proceed] SSM health check failed: {e}")
                ssm_health = {'recommendation': 'UNKNOWN', 'error': str(e)}
            try:
                alarm_health = future_alarms.result(timeout=15)
            except TimeoutError:
                logger.warning("[TOOL:verify_and_proceed] Worker timed out for CloudWatch alarm check")
                alarm_health = {'recommendation': 'UNKNOWN', 'error': 'timeout'}
            except Exception as e:
                logger.warning(f"[TOOL:verify_and_proceed] CloudWatch alarm check failed: {e}")
                alarm_health = {'recommendation': 'UNKNOWN', 'error': str(e)}
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        # Alarms that evaluate over longer than the minimum settle need that long after the run.
        settle = _settle_seconds(alarm_health)
        if elapsed is not None and settle - elapsed > 0:
            wait = int(settle - elapsed)
            return {'status': 'SETTLING', 'recommendation': 'WAIT', 'retry_after_seconds': wait,
                    'reason': f"Run finished {int(elapsed)}s ago; these instances' alarms need about "
                              f"{settle // 60} minutes to evaluate. Check again in {wait}s."}

        # Determine recommendation. Only a clean result from both checks
        # proceeds; anything else holds the rollout for the operator.
        ssm_rec = ssm_health.get('recommendation')
        alarm_rec = alarm_health.get('recommendation')
        if ssm_rec == 'ROLLBACK_RECOMMENDED':
            return {
                'status': 'UNHEALTHY',
                'recommendation': 'ROLLBACK',
                'reason': 'SSM health check failed',
                'ssm_health': ssm_health,
                'alarm_health': alarm_health
            }

        if alarm_health.get('unhealthy_instances', 0) > 0:
            return {
                'status': 'UNHEALTHY',
                'recommendation': 'HOLD',
                'reason': (
                    f"CloudWatch alarms firing on {alarm_health['unhealthy_instances']} "
                    "instance(s). Do not promote until the operator has reviewed them."
                ),
                'ssm_health': ssm_health,
                'alarm_health': alarm_health
            }

        no_data = _no_data_since(alarm_health, started_at)
        if no_data:
            return {
                'status': 'NEEDS_REVIEW',
                'recommendation': 'HOLD',
                'reason': (f"{len(no_data)} alarm(s) went to INSUFFICIENT_DATA after the run started "
                           f"({', '.join(no_data[:5])}). The metric may have stopped reporting because of "
                           "the run; review them before promoting."),
                'ssm_health': ssm_health,
                'alarm_health': alarm_health
            }

        if unlisted:
            # Instances there weren't checked at all (disruption round, 3 Nov: PROCEED with one left out).
            return {
                'status': 'NEEDS_REVIEW',
                'recommendation': 'HOLD',
                'reason': (f"Instances in {', '.join(unlisted[:3])} couldn't be listed, so Patchy can't confirm "
                           "every instance this run reached is healthy."),
                'unreadable_locations': unlisted,
                'ssm_health': ssm_health,
                'alarm_health': alarm_health
            }

        if ssm_rec != 'HEALTHY' or alarm_rec != 'HEALTHY':
            return {
                'status': 'NEEDS_REVIEW',
                'recommendation': 'HOLD',
                'reason': f'Health not confirmed (SSM: {ssm_rec}, alarms: {alarm_rec})',
                'ssm_health': ssm_health,
                'alarm_health': alarm_health
            }

        _record_verified_stage(automation_execution_id, environment, ended_at)
        return {
            'status': 'HEALTHY',
            'recommendation': 'PROCEED',
            'reason': f'{environment} environment verified healthy',
            'checked_instance_ids': instance_ids,
            'ssm_health': ssm_health,
            'alarm_health': alarm_health
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"Error in verify_and_proceed: {error_info}")
        error_info["recommendation"] = "INVESTIGATE"
        return error_info
    except Exception as e:
        logger.error(f"Error in verify_and_proceed: {e}")
        result = classify_error(e)
        result["recommendation"] = "INVESTIGATE"
        return result
