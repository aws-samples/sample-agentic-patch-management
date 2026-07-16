"""Maintenance window, Quick Setup patch policy association, health check, and verification tools."""

from strands import tool
from typing import Optional, List, Dict, Any

from . import _shared
from ._shared import (
    logger, os, time,
    classify_error,
    ClientError,
    datetime, timedelta, timezone,
    get_client, get_hub_account_id,
    _ssm, _cloudwatch,
    _validate_instance_ids, _validate_instance_scope,
    _normalize_environment,
    _get_configured_scope_accounts,
    _get_query_targets,
    _get_fleet_summary,
    _direct_describe_scoped_instances,
    _format_utc_as_local,
    AWS_REGION,
    SCOPE_TAG_KEY, SCOPE_TAG_VALUE,
)


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


def _get_maintenance_windows_for_target(account_id: str, region: str,
                                        env_value: Optional[str],
                                        instance_ids: Optional[list],
                                        instance_tags: Dict[str, Dict[str, str]]) -> List[dict]:
    """Per-(account, region) maintenance window lookup. Returns enriched window dicts."""
    try:
        ssm_client = get_client('ssm', account_id=account_id, region=region)
    except Exception as e:
        logger.warning(f"[TOOL:get_maintenance_windows] SSM client failed for {account_id}/{region}: {e}")
        return []

    try:
        window_identities = []
        paginator = ssm_client.get_paginator('describe_maintenance_windows')
        for page in paginator.paginate():
            window_identities.extend(page.get('WindowIdentities', []))
    except Exception as e:
        logger.warning(f"[TOOL:get_maintenance_windows] describe_maintenance_windows failed for {account_id}/{region}: {e}")
        return []

    windows: List[dict] = []
    for window_identity in window_identities:
        window_id = window_identity['WindowId']
        try:
            window_details = ssm_client.get_maintenance_window(WindowId=window_id)
            tasks_response = ssm_client.describe_maintenance_window_tasks(WindowId=window_id)
            has_patch_task = any(
                t.get('TaskArn') == 'AWS-RunPatchBaseline'
                for t in tasks_response.get('Tasks', [])
            )

            targets_response = ssm_client.describe_maintenance_window_targets(WindowId=window_id)

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
            window_environment = 'unknown'

            for target in targets_response.get('Targets', []):
                target_filters = target.get('Targets', [])
                target_list.append({
                    'resource_type': target.get('ResourceType'),
                    'filters': target_filters,
                })
                for filter_item in target_filters:
                    if filter_item.get('Key') == 'tag:Environment':
                        values = filter_item.get('Values', [])
                        if values:
                            window_environment = values[0]
                    if instance_ids and instance_tags:
                        for iid in instance_ids:
                            tags = instance_tags.get(iid, {})
                            if _match_association_to_instance(target_filters, iid, tags):
                                if iid not in instances_covered:
                                    instances_covered.append(iid)

            if env_value and window_environment != env_value:
                continue

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
            continue

    return windows


# ============================================================================
# TOOLS
# ============================================================================


@tool
def get_maintenance_windows(environment: str, instance_ids: list = None) -> dict:
    """Get patch-related maintenance windows across all configured accounts and regions.

    Decision: Use when operator asks about maintenance windows, scheduling, or
    next available patch window. When planning or handling a CVE, ALWAYS also
    call get_patch_policy — windows define WHEN, policies define WHAT and HOW OFTEN.

    Maintenance windows are SSM resources, scoped per region. We fan out across
    every (account, region) target so windows in spoke regions aren't missed.
    Each returned window includes its account_id and region.

    Args:
        environment: Filter by environment (dev, staging, prod) - REQUIRED
        instance_ids: Optional list of instance IDs to validate coverage

    Returns:
        dict: {'windows': list, 'count': int, 'environment_filter': str,
               'queried_targets': list}
    """
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
            cache = _shared._fleet_instances_cache or {}
            unresolved = [iid for iid in instance_ids if iid not in cache]
            for iid, info in cache.items():
                if iid in instance_ids:
                    # Synthesise a tags dict from cached fields the agent might inspect
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

        from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
        with _TPE(max_workers=min(len(targets) or 1, 10)) as executor:
            futures = {
                executor.submit(_get_maintenance_windows_for_target, a, r, env_value, instance_ids, instance_tags): (a, r)
                for (a, r) in targets
            }
            for fut in _ac(futures):
                acct, rgn = futures[fut]
                try:
                    all_windows.extend(fut.result(timeout=15))
                except TimeoutError:
                    logger.warning(f"[TOOL:get_maintenance_windows] Worker timed out for {acct}/{rgn}")
                except Exception as e:
                    logger.warning(f"[TOOL:get_maintenance_windows] {acct}/{rgn} failed: {e}")

        return {
            'windows': all_windows,
            'count': len(all_windows),
            'environment_filter': environment if environment else 'all',
            'instances_validated': len(instance_ids) if instance_ids else 0,
            'queried_targets': [{'account_id': a, 'region': r} for (a, r) in targets],
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


@tool
def create_maintenance_window(name: str, schedule: str, duration: int,
                              target_environment: str) -> dict:
    """Create AWS Systems Manager maintenance window.

    Args:
        name: Name for the maintenance window
        schedule: Cron expression for schedule (e.g., 'cron(0 2 ? * TUE *)')
        duration: Duration in hours
        target_environment: Environment to target (dev, staging, prod)

    Returns:
        dict: {'window_id': str, 'name': str, 'schedule': str}
    """
    try:
        # Tag-on-create: the IAM policy in core-stack.ts requires
        # ManagedBy=IntelligentPatchAutomation on creation. The same tag is
        # used by the Modify policy to gate update/delete operations on this
        # window. Without the tag, the agent cannot manage the window it just
        # created (closed-loop tag-based scoping).
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
        _ssm().register_target_with_maintenance_window(
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

        return {
            'window_id': window_id,
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
    """Check Quick Setup patch policy associations (SSM State Manager) for instances.

    Decision: ALWAYS call this when operator asks to PLAN, schedule, or handle a
    CVE. Quick Setup patch policy associations define WHAT gets patched (baseline,
    severity, classification) and on WHAT schedule. Call alongside
    get_maintenance_windows — windows define WHEN, policies define WHAT and HOW OFTEN.

    Uses list-associations (1 API call, cached) + client-side tag matching.
    Scales to any fleet size without per-instance API calls.

    Args:
        instance_ids: List of EC2 instance IDs to check
        environment: Optional environment filter for context

    Returns:
        dict with per-instance patch policy info:
        {
            'instance_policies': {
                'i-abc': [{'association_id': ..., 'name': ..., 'operation': ..., 'schedule': ...}],
                'i-def': []  # no patch policy
            },
            'instances_without_policy': ['i-def'],
            'instances_with_install_policy': ['i-abc'],
            'summary': '4 of 5 instances have a patch install policy'
        }
    """
    try:
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error

        logger.info(f"[TOOL:get_patch_policy] instances={len(instance_ids)} environment={environment}")

        # Get instance tags (we need these for target matching).
        # Use _direct_describe_scoped_instances which fans out across all
        # configured (account, region) pairs -- handles spoke instances.
        instance_tags_map: Dict[str, Dict[str, str]] = {}
        try:
            described = _direct_describe_scoped_instances(instance_ids=instance_ids)
            for iid, info in described.items():
                instance_tags_map[iid] = info.get('tags', {})
        except Exception as e:
            logger.warning(f"Could not get instance tags via cross-account describe: {e}")

        # Get cached + enriched patch associations (cached 5 min)
        enriched_assocs = _get_enriched_patch_associations()

        # Match associations to instances
        instance_policies: Dict[str, List[dict]] = {iid: [] for iid in instance_ids}
        for assoc in enriched_assocs:
            for iid in instance_ids:
                tags = instance_tags_map.get(iid, {})
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
    """Check instance health after patching operations.

    Args:
        instance_ids: List of EC2 instance IDs to check

    Returns:
        dict: {'health_results': list, 'summary': dict, 'overall_health': str}
    """
    try:
        # Validate instance IDs
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error

        # Group instances by (account, region) for cross-region support
        by_location: Dict[tuple, list] = {}
        cache = _shared._fleet_instances_cache or {}
        for iid in instance_ids:
            cached = cache.get(iid)
            if cached:
                loc = (cached.get('account_id'), cached.get('region') or AWS_REGION)
            else:
                loc = (None, AWS_REGION)
            by_location.setdefault(loc, []).append(iid)

        # Query SSM per (account, region) -- describe_instance_information is regional
        instance_info_map: Dict[str, dict] = {}
        for (account_id, region), iids in by_location.items():
            try:
                ssm_client = get_client('ssm', account_id=account_id, region=region)
                ssm_info = ssm_client.describe_instance_information(
                    Filters=[{'Key': 'InstanceIds', 'Values': iids}]
                )
                for info in ssm_info.get('InstanceInformationList', []):
                    instance_info_map[info['InstanceId']] = info
            except Exception as e:
                logger.warning(f"[TOOL:check_instance_health] SSM query failed for {account_id}/{region}: {e}")

        health_results = []
        for instance_id in instance_ids:
            instance_info = instance_info_map.get(instance_id)

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

        # Calculate overall health
        avg_health = sum(r['health_score'] for r in health_results) / len(health_results) if health_results else 0
        unhealthy_count = sum(1 for r in health_results if r['health_score'] < 100)

        logger.info(f"[TOOL:check_instance_health] RESULT: instances={len(instance_ids)} avg_health={avg_health:.0f} unhealthy={unhealthy_count}")

        return {
            'results': health_results,
            'overall_health': avg_health,
            'total_instances': len(instance_ids),
            'unhealthy_instances': unhealthy_count,
            'recommendation': 'HEALTHY' if avg_health >= 80 else 'INVESTIGATE' if avg_health >= 50 else 'ROLLBACK_RECOMMENDED'
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"Error checking instance health: {error_info}")
        error_info["instance_ids"] = instance_ids
        return error_info
    except Exception as e:
        logger.error(f"Error checking instance health: {e}")
        return classify_error(e)


@tool
def check_cloudwatch_alarms(instance_ids: List[str], alarm_name_pattern: Optional[str] = None) -> dict:
    """Check CloudWatch alarms for EC2 instances.

    Args:
        instance_ids: List of EC2 instance IDs to check alarms for
        alarm_name_pattern: Optional pattern to filter alarm names (e.g., 'httpd', 'nginx')

    Returns:
        dict: {'alarms': list, 'summary': dict, 'overall_status': str}
    """
    try:
        # Validate instance IDs
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error

        # Group instances by (account, region) for cross-region support
        cache = _shared._fleet_instances_cache or {}
        by_location: Dict[tuple, list] = {}
        for iid in instance_ids:
            cached = cache.get(iid)
            if cached:
                loc = (cached.get('account_id'), cached.get('region') or AWS_REGION)
            else:
                loc = (None, AWS_REGION)
            by_location.setdefault(loc, []).append(iid)

        # Fetch alarms per (account, region) -- CloudWatch is regional
        alarms_in_alarm: list = []
        for (account_id, region), iids in by_location.items():
            try:
                cw_client = get_client('cloudwatch', account_id=account_id, region=region)
                paginator = cw_client.get_paginator('describe_alarms')
                for page in paginator.paginate(StateValue='ALARM', MaxRecords=100):
                    alarms_in_alarm.extend(page.get('MetricAlarms', []))
            except Exception as e:
                logger.warning(f"[TOOL:check_cloudwatch_alarms] CW query failed for {account_id}/{region}: {e}")

        alarm_results = []
        for instance_id in instance_ids:
            instance_alarms = []
            for alarm in alarms_in_alarm:
                dimensions = alarm.get('Dimensions', [])
                has_instance = any(
                    d['Name'] == 'InstanceId' and d['Value'] == instance_id
                    for d in dimensions
                )
                if has_instance:
                    if alarm_name_pattern is None or alarm_name_pattern.lower() in alarm['AlarmName'].lower():
                        instance_alarms.append({
                            'name': alarm['AlarmName'],
                            'state': alarm['StateValue'],
                            'reason': alarm.get('StateReason', 'Unknown'),
                            'timestamp': str(alarm.get('StateUpdatedTimestamp', 'N/A'))
                        })

            # Limit to 5 most recent alarms to avoid token bloat
            instance_alarms = instance_alarms[:5]

            alarm_results.append({
                'instance_id': instance_id,
                'alarm_count': len(instance_alarms),
                'alarms': instance_alarms,
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
            'recommendation': 'HEALTHY' if unhealthy_count == 0 else 'INVESTIGATE',
            'message': (
                f'{unhealthy_count} instances have alarms firing ({total_alarms} total alarms). '
                'Check alarm timestamps -- pre-existing alarms may not be related to the current operation.'
                if unhealthy_count > 0
                else 'All instances healthy - no alarms firing'
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


@tool
def verify_and_proceed(command_id: str, instance_ids: List[str],
                       environment: str, alarm_pattern: Optional[str] = "httpd") -> dict:
    """Verify patch completion and health before proceeding to next environment.

    This is an OPTIONAL tool for production-ready verification. Use when:
    - User explicitly requests verification ("verify and proceed")
    - Patching production environment (higher safety requirements)
    - User wants to wait for completion before moving forward

    For development/testing environments, users can say "proceed" without verification.

    Args:
        command_id: SSM command ID from execute_patch_operation
        instance_ids: List of instance IDs that were patched
        environment: Environment name (for logging)
        alarm_pattern: CloudWatch alarm pattern to check (default: "httpd")

    Returns:
        dict: {'status': str, 'recommendation': str, 'details': dict}
    """
    try:
        # Validate instance IDs
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error

        # Poll command status until complete (max 10 minutes)
        from .patch_tools import get_command_status

        max_attempts = 60  # 10 min with 10s intervals
        attempt = 0

        while attempt < max_attempts:
            status_result = get_command_status(command_id, instance_ids)

            if status_result.get('status') == 'Success':
                # Wait 2 minutes for CloudWatch alarm evaluation
                logger.info("Patching complete. Waiting 2 minutes for alarm evaluation...")
                time.sleep(120)  # nosemgrep: arbitrary-sleep
                break
            elif status_result.get('status') == 'Failed':
                return {
                    'status': 'FAILED',
                    'recommendation': 'ROLLBACK',
                    'reason': 'Patch command failed',
                    'details': status_result
                }

            # Still in progress
            attempt += 1
            if attempt < max_attempts:
                time.sleep(10)  # nosemgrep: arbitrary-sleep

        if attempt >= max_attempts:
            return {
                'status': 'TIMEOUT',
                'recommendation': 'INVESTIGATE',
                'reason': 'Patch command did not complete within 10 minutes',
                'details': status_result
            }

        # Run health checks in parallel (independent API calls)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as executor:
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

        # Determine recommendation
        if ssm_health.get('recommendation') == 'ROLLBACK_RECOMMENDED':
            return {
                'status': 'UNHEALTHY',
                'recommendation': 'ROLLBACK',
                'reason': 'SSM health check failed',
                'ssm_health': ssm_health,
                'alarm_health': alarm_health
            }

        if alarm_health.get('recommendation') == 'ROLLBACK_RECOMMENDED':
            return {
                'status': 'UNHEALTHY',
                'recommendation': 'ROLLBACK',
                'reason': 'CloudWatch alarms firing',
                'ssm_health': ssm_health,
                'alarm_health': alarm_health
            }

        return {
            'status': 'HEALTHY',
            'recommendation': 'PROCEED',
            'reason': f'{environment} environment verified healthy',
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
