"""Patch management tools: dry-run, execute, rollback, status, multi-account operations."""

from strands import tool
from datetime import timedelta
from typing import Optional, List, Dict, Any, Tuple

from . import _shared
from ._shared import (
    logger, classify_error,
    ClientError,
    datetime, timezone,
    get_client, _ssm, _validate_instance_ids, _validate_instance_scope, _validate_account_scope, _validate_regions,
    _instance_environments,
    _normalize_environment, _environment_tag_values,
    _get_query_targets,
    _get_fleet_summary,
    _group_instances_by_location,
    _resolve_sla_for_instances,
    _window_check,
    _cve_severity,
    _window_check_text,
    _get_baseline_override_url,
    reboot_option as _reboot_option,
    _rollback_window_params,
    _with_location_alarms, run_limits, _client_token, _started,
    _earliest_first_observed_at,
    _write_pending_compliance_context,
    _unwrap_patch_state,
    _start_patch_automation,
    _start_instance_patch_automation,
    _start_instance_rollback_automation,
    _format_utc_as_local,
    _calculate_sla_requirement,
    _current_request_scans,
    get_operator, get_session_id,
    AWS_REGION,
    SCOPE_TAG_KEY, SCOPE_TAG_VALUE,
    ROLLBACK_DOC_NAME, EXECUTION_DEFAULTS, SPOKE_EXECUTION_ROLE, SPOKE_REGIONS, execution_limits,
)

# Threads for per-(account, region) discovery, and for reading one command's invocations.
_DISCOVERY_WORKERS = 20
_COMMAND_STATUS_WORKERS = 10


# ============================================================================
# TOOLS
# ============================================================================


@tool
def get_patch_compliance(environment: str,
                        instance_ids: Optional[List[str]] = None,
                        severity: Optional[str] = None) -> dict:
    """Current SSM patch compliance (missing and installed counts) for an environment or instances.

    Use for current state: "how compliant is prod", "what is missing on i-xxx".
    For past runs and SLA outcomes use query_compliance_reports.

    Args:
        environment: dev, staging or prod.
        instance_ids: Specific instances.
        severity: Adds the SLA for this severity from instance tags.
    """
    # Environment mode discovers running instances tagged Environment=<env> and the
    # scope tag across [hub + spokes] x SPOKE_REGIONS; ID mode locates each instance via
    # the fleet cache. Patch data exists only for instances that have been scanned.
    try:
        instance_tags: Dict[str, Dict[str, str]] = {}
        # instance_locations[iid] = (account_id, region) -- populated during discovery
        instance_locations: Dict[str, tuple] = {}
        env_value = _normalize_environment(environment) if environment else None
        logger.info(f"[TOOL:get_patch_compliance] environment={environment} "
                    f"instance_ids={instance_ids[:5] if instance_ids else 'auto'} severity={severity}")

        # -- Mode 1: env-driven discovery across all (account, region) pairs --
        discovery_unreadable: List[str] = []
        if not instance_ids:
            if not env_value:
                return {
                    "error": "environment is required when instance_ids is not provided",
                    "instances": [], "total_count": 0, "ec2_instance_ids": [],
                }
            instance_ids = []
            targets = _get_query_targets()
            # Every spelling the environment's instances carry (prod, Production), not just
            # the normalised name (2 Nov review, L6).
            env_tags = _environment_tag_values(env_value) or [env_value]

            def _discover_in_target(account_id: str, region: str) -> List[dict]:
                """Return [{'instance_id', 'tags', 'account_id', 'region'}, ...]"""
                try:
                    ec2 = get_client('ec2', account_id=account_id, region=region)
                except Exception as e:
                    logger.warning(f"[TOOL:get_patch_compliance] could not get EC2 client for {account_id}/{region}: {e}")
                    discovery_unreadable.append(f"{account_id}/{region}")
                    return []
                discovered: List[dict] = []
                try:
                    paginator = ec2.get_paginator('describe_instances')
                    for page in paginator.paginate(Filters=[
                        {'Name': 'tag:Environment', 'Values': env_tags},
                        {'Name': f'tag:{SCOPE_TAG_KEY}', 'Values': [SCOPE_TAG_VALUE]},
                        {'Name': 'instance-state-name', 'Values': ['running']},
                    ]):
                        for reservation in page['Reservations']:
                            for instance in reservation['Instances']:
                                tags = {t['Key']: t['Value'] for t in instance.get('Tags', [])}
                                discovered.append({
                                    'instance_id': instance['InstanceId'],
                                    'tags': tags,
                                    'account_id': account_id,
                                    'region': region,
                                })
                except Exception as e:
                    logger.warning(f"[TOOL:get_patch_compliance] discovery failed for {account_id}/{region}: {e}")
                    discovery_unreadable.append(f"{account_id}/{region}")
                return discovered

            from concurrent.futures import as_completed as _ac
            from helper.concurrency import ContextThreadPoolExecutor as _TPE
            with _TPE(max_workers=min(len(targets) or 1, _DISCOVERY_WORKERS)) as executor:
                futures = {executor.submit(_discover_in_target, a, r): (a, r) for (a, r) in targets}
                for fut in _ac(futures):
                    acct, rgn = futures[fut]
                    try:
                        discovered = fut.result()
                    except Exception as e:
                        logger.warning(f"[TOOL:get_patch_compliance] Worker failed for {acct}/{rgn}: {e}")
                        continue
                    for d in discovered:
                        iid = d['instance_id']
                        instance_ids.append(iid)
                        instance_locations[iid] = (d['account_id'], d['region'])
                        if severity:
                            instance_tags[iid] = d['tags']

            # De-dup (an instance ID is unique within a region, but the same ID *could*
            # theoretically appear in two accounts -- extremely unlikely, but we guard against it)
            instance_ids = list(dict.fromkeys(instance_ids))

        # -- Mode 2: id-driven -- look up locations from the fleet cache --
        else:
            # Explorer cache, then a direct describe across the configured targets.
            for loc, iids in _shared._locate_instances(instance_ids).items():
                for iid in iids:
                    instance_locations[iid] = loc

        if not instance_ids:
            empty = {"instances": [], "total_count": 0, "ec2_instance_ids": []}
            if discovery_unreadable:
                empty.update(unreadable_locations=discovery_unreadable,
                             next_action=(f"Say that instances in {', '.join(discovery_unreadable[:3])} couldn't be "
                                          "read, so Patchy can't say whether there are any there."))
            return empty

        # -- Group by (account, region) and call describe_instance_patch_states per group --
        by_loc: Dict[tuple, List[str]] = {}
        for iid in instance_ids:
            by_loc.setdefault(instance_locations.get(iid, ('', AWS_REGION)), []).append(iid)

        from helper.retry import throttled
        instances: List[dict] = []
        ssm_found_ids: set = set()
        # Instances whose patch state couldn't be read: not known to be unscanned (disruption round, 2 Nov).
        state_unreadable: set = set()
        for (acct, rgn), ids in by_loc.items():
            try:
                ssm_client = get_client('ssm', account_id=acct or None, region=rgn)
            except Exception as e:
                logger.warning(f"[TOOL:get_patch_compliance] could not get SSM client for {acct}/{rgn}: {e}")
                state_unreadable.update(ids)
                continue
            for i in range(0, len(ids), 50):
                batch = ids[i:i+50]
                try:
                    response = throttled(lambda: ssm_client.describe_instance_patch_states(InstanceIds=batch))
                except Exception as e:
                    logger.warning(f"[TOOL:get_patch_compliance] patch-state lookup failed for {acct}/{rgn} batch: {e}")
                    state_unreadable.update(batch)
                    continue
                for state in response.get('InstancePatchStates', []):
                    ssm_found_ids.add(state['InstanceId'])
                    instances.append({
                        'instance_id': state['InstanceId'],
                        'account_id': acct,
                        'region': rgn,
                        'patch_group': state.get('PatchGroup', 'N/A'),
                        'installed_count': state.get('InstalledCount', 0),
                        'missing_count': state.get('MissingCount', 0),
                        'failed_count': state.get('FailedCount', 0),
                        'security_non_compliant_count': state.get('SecurityNonCompliantCount', 0),
                        'operation': state.get('Operation', 'N/A'),
                        'operation_start_time': state.get('OperationStartTime', '').isoformat() if state.get('OperationStartTime') else 'N/A',
                        'operation_end_time': _format_utc_as_local(state.get('OperationEndTime', '').isoformat()) if state.get('OperationEndTime') else 'N/A',
                        'reboot_option': state.get('RebootOption', 'N/A'),
                    })

        unscanned_ids = [iid for iid in instance_ids if iid not in ssm_found_ids and iid not in state_unreadable]

        _s_total_missing = sum(i['missing_count'] for i in instances)
        _s_needing = sum(1 for i in instances if i['missing_count'] > 0)
        _s_compliant = len(instances) - _s_needing
        _s_summary = f"{len(instances)} instances, {_s_needing} need patches ({_s_total_missing} missing). {_s_compliant} compliant." if instances else "0 instances found."

        result = {
            "summary": _s_summary,
            "instances": instances,
            "total_count": len(instances),
            "ec2_instance_ids": instance_ids,
            "ec2_instance_count": len(instance_ids),
            "compliance_summary": {
                "total_missing": _s_total_missing,
                "total_security_non_compliant": sum(i['security_non_compliant_count'] for i in instances),
                "instances_needing_patches": _s_needing,
            },
            "queried_targets": [{"account_id": a, "region": r} for (a, r) in by_loc.keys()],
            "next_action": "Present compliance status to the operator. If patching is needed, call resolve_execution_scope then multi_account_execute (fleet) or execute_patch_operation (named instances).",
        }

        if unscanned_ids:
            result['unscanned_instance_ids'] = unscanned_ids
        if state_unreadable:
            result['patch_state_unreadable_instance_ids'] = sorted(state_unreadable)
            result['next_action'] = (f"Say that the patch state of {len(state_unreadable)} instance(s) couldn't be "
                                     "read, so they aren't in these figures and aren't known to be unscanned. "
                                     + result['next_action'])
        if discovery_unreadable:
            # Instances there weren't counted at all; they aren't compliant or unscanned (2 Nov review, L2).
            result['unreadable_locations'] = discovery_unreadable
            result['next_action'] = (f"Say that instances in {', '.join(discovery_unreadable[:3])} couldn't be read, "
                                     "so they aren't in these figures. " + result['next_action'])

        logger.info(f"[TOOL:get_patch_compliance] RESULT: ec2_count={len(instance_ids)} "
                    f"ssm_count={len(instances)} total_missing={result['compliance_summary']['total_missing']} "
                    f"needing_patches={result['compliance_summary']['instances_needing_patches']} "
                    f"locations={len(by_loc)}"
                    f"{' unscanned=' + str(len(unscanned_ids)) if unscanned_ids else ''}")

        # Calculate SLA if severity provided -- use the strictest framework set across ALL instances
        if severity and instance_tags:
            all_frameworks: set = set()
            for tags in instance_tags.values():
                frameworks_str = tags.get('ComplianceFrameworks', '')
                if frameworks_str:
                    all_frameworks.update(f.strip() for f in frameworks_str.split(',') if f.strip())
            if all_frameworks:
                sla_req = _calculate_sla_requirement(list(all_frameworks), severity)
                if sla_req:
                    result['sla_requirement'] = sla_req

        return result
    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"Error getting patch compliance: {error_info}")
        error_info["instances"] = []
        error_info["total_count"] = 0
        return error_info
    except Exception as e:
        logger.error(f"Error getting patch compliance: {e}")
        result = classify_error(e)
        result["instances"] = []
        result["total_count"] = 0
        return result


@tool
def patch_dry_run(instance_ids: List[str], severity_filter: Optional[str] = None) -> dict:
    """Scan instances for missing patches without installing anything.

    Use only when the operator asks to scan, preview or dry-run named instances. Don't call it
    before patching; the patch plan is the preview.

    Args:
        instance_ids: Instances to scan.
        severity_filter: CRITICAL, HIGH, MEDIUM or LOW; that level and above.
    """
    # Runs Patchy-RunPatchBaselineById with TargetLocations (cross-account and
    # cross-region) and polls until it completes.
    try:
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error
        scope_error = _validate_instance_scope(instance_ids)
        if scope_error:
            return scope_error

        logger.info(f"[TOOL:patch_dry_run] instances={len(instance_ids)} severity_filter={severity_filter} instance_ids={instance_ids[:5]}")

        # Severity override validation
        if severity_filter and not _get_baseline_override_url(severity_filter):
            return {
                "status": "warning",
                "result_type": "error",
                "warning": f"Severity filter '{severity_filter}' requested but the baseline "
                           "override file is missing from S3. This means the scan would use "
                           "the default baseline (ALL severities) instead of just "
                           f"{severity_filter} and above.",
                "options": [
                    f"Retry without severity filter: patch_dry_run(instance_ids={instance_ids})",
                    "Fix the overrides: run setup_baseline_overrides.py on the deployment machine"
                ],
                "severity_filter_requested": severity_filter,
                "severity_filter_applied": False,
                "next_action": "The operation was NOT started. Present this to the operator.",
            }

        # Group instances by their owning (account, region) so each Automation
        # only targets the instances that actually live in that location.
        # Without this, a single Automation fans out to all (account, region)
        # children and each child receives every instance ID -- causing scans
        # against the wrong accounts to silently fail.
        groups = _group_instances_by_location(instance_ids)

        execution_ids: List[str] = []
        executed_account_ids: List[str] = []
        executed_regions: List[str] = []
        for (account, region), group_iids in groups.items():
            exec_id = _start_instance_patch_automation(
                operation='Scan',
                instance_ids=group_iids,
                account_ids=[account],
                regions=[region],
                max_concurrency=EXECUTION_DEFAULTS['account_max_concurrency'],
                max_errors=EXECUTION_DEFAULTS['account_max_errors'],
                severity_filter=severity_filter,
            )
            execution_ids.append(exec_id)
            executed_account_ids.append(account)
            executed_regions.append(region)
            logger.info(f"[TOOL:patch_dry_run] Automation started: account={account} "
                        f"region={region} instances={len(group_iids)} exec_id={exec_id}")

        # Track all scans for operator confirmation gate
        scans = _current_request_scans.get()
        if scans is not None:
            for eid in execution_ids:
                scans.add(eid)

        # Backwards-compat: preserve the old `automation_execution_id` field
        # by reporting the first execution. Add `automation_execution_ids` for
        # the full list.
        execution_id = execution_ids[0] if execution_ids else ''
        account_ids = sorted(set(executed_account_ids))
        target_regions = sorted(set(executed_regions))

        override_url = _get_baseline_override_url(severity_filter)
        logger.info(f"[TOOL:patch_dry_run] RESULT: instances={len(instance_ids)} "
                     f"severity_filter={severity_filter} executions={len(execution_ids)} "
                     f"exec_ids={execution_ids}")

        return {
            'automation_execution_id': execution_id,
            'automation_execution_ids': execution_ids,
            'total_instances': len(instance_ids),
            'instance_ids': instance_ids,
            'account_ids': account_ids,
            'regions': target_regions,
            'severity_filter': severity_filter,
            'baseline_override': override_url,
            'severity_filter_applied': bool(override_url) if severity_filter else None,
            'status': 'SCAN_INITIATED',
            'result_type': 'execution_started',
            'operation': 'scan',
            'estimated_duration': '2-3 minutes',
            'next_action': f"Started. Say what started and give execution ID {execution_id} exactly; it runs in the background and the operator can ask for status. Then stop.",
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"Error running patch dry-run: {error_info}")
        error_info["instances"] = []
        return error_info
    except Exception as e:
        logger.error(f"Error running patch dry-run: {e}")
        result = classify_error(e)
        result["instances"] = []
        return result


def _environment_instance_ids(environment: str, account_ids: List[str]) -> List[str]:
    """Scoped instances in an environment across the given accounts: the fleet view, and new ones from EC2."""
    return list(_shared.environment_instances(environment, account_ids or None))


def _environment_target(environment: str, account_ids: List[str]) -> Tuple[str, Optional[dict]]:
    """The tag value a tag-based runbook should match for this environment, or a refusal.

    One value per run: if instances in these accounts carry several spellings
    (prod and Production), one runbook can't reach them all.
    """
    values = _environment_tag_values(environment, account_ids)
    if len(values) > 1:
        return environment, {
            "error": (f"Instances in {environment} are tagged Environment=" + " and Environment=".join(values)
                      + "; a run matches one value."),
            "error_code": "MixedEnvironmentTags",
            "result_type": "gate_blocked",
            "category": "ABORT",
            "retryable": False,
            "tag_values": values,
            "next_action": "Nothing was planned. Tell the operator to retag the instances to one value.",
        }
    return (values[0] if values else environment), None


def _locations_allowed(concurrency: str, locations: int) -> int:
    """How many (account, region) locations a policy's account concurrency allows at once."""
    value = str(concurrency).strip()
    if value.endswith('%'):
        return max(1, (locations * int(value[:-1])) // 100)
    return max(1, int(value))


def _location_limit(environment: str, groups: dict, tool: str) -> Optional[dict]:
    """Refuse an instance-ID run whose locations exceed what the environment allows at once.

    Each (account, region) of an instance-ID run is its own Automation, started
    together, so SSM can't hold them to "one account at a time" or stop the
    others on the first failure. The tag-based tools run as one Automation and
    can.
    """
    limits = execution_limits(environment)
    allowed = _locations_allowed(limits['account_max_concurrency'], len(groups))
    if len(groups) <= allowed:
        return None
    where = ", ".join(f"{a}/{r}" for a, r in sorted(groups))
    return {
        "error": (f"These instances are in {len(groups)} accounts or regions ({where}); the {environment} policy "
                  f"allows {limits['account_max_concurrency']} at once with up to {limits['account_max_errors']} "
                  "failed, which a run by instance ID can't enforce across locations."),
        "error_code": "LocationLimit",
        "result_type": "gate_blocked",
        "category": "ABORT",
        "retryable": False,
        "locations": [f"{a}/{r}" for a, r in sorted(groups)],
        "next_action": (f"Nothing was planned. Offer the environment-wide tool instead (multi_account_execute or "
                        f"multi_account_rollback), which keeps the policy, or one {tool} plan per account and region."),
    }


_SEVERITY_RUNS_NOW = ("Severity plans run now: each finding's SLA runs from when it was first seen, so no one "
                      "maintenance window can be shown to meet them all. To check windows and patch policies "
                      "against a deadline, plan by CVE.")


# Why a run went ahead now rather than waiting for a schedule, as recorded in its report
# (2 Nov review, M16). The console words each one (ReportsView).
RUN_NOW_REASONS = ("operator_asked", "severity_runs_now", "no_sla", "schedules_unchecked",
                   "no_schedule_meets_sla", "some_targets_uncovered")


def _run_now_reason(force_emergency: bool, sla_hours, severity_filter: Optional[str],
                    coverage: Optional[dict], checked: bool = True) -> Optional[str]:
    """The RUN_NOW_REASONS entry for a run that goes ahead now. checked=False when the schedule
    check didn't run here (the plan ran it); None then means not known from this call."""
    if force_emergency:
        return "operator_asked"
    if not sla_hours:
        return "severity_runs_now" if severity_filter else "no_sla"
    if not checked:
        return (_shared._plan_controls.get() or {}).get("run_now_reason")
    if coverage is None:
        return "schedules_unchecked"
    return ("some_targets_uncovered" if any(r.get("covered") for r in coverage.get("rows") or [])
            else "no_schedule_meets_sla")


# The same reasons in words the model repeats to the operator (the plan's why_now).
_WHY_NOW = {
    "operator_asked": "you asked to patch now",
    "severity_runs_now": "severity plans run now, since no one maintenance window can be shown to meet every finding's SLA",
    "no_sla": "no SLA applies, so no schedule was checked",
    "schedules_unchecked": "the maintenance windows and patch policies couldn't be checked against the SLA",
    "no_schedule_meets_sla": "no maintenance window or patch policy would patch the targets before the SLA deadline",
    "some_targets_uncovered": "some targets aren't covered by a maintenance window or patch policy that meets the SLA",
}


def _why_now(reason: Optional[str]) -> str:
    return _WHY_NOW.get(reason or "", "no maintenance window covers these targets before the SLA deadline, or you asked to patch now")


def _inspector_unreadable(cve_id: str, e: Exception) -> dict:
    return {"error": f"Inspector couldn't be read for {cve_id}, so its severity and SLA aren't known.",
            "error_code": "InspectorUnreadable", "result_type": "error", "detail": str(e)[:200],
            "retryable": True,
            "next_action": "The operation was NOT started. Say so, and suggest trying again in a few minutes."}


def _coverage_check(*args, **kwargs) -> dict:
    from helper import coverage
    return coverage.check(*args, **kwargs)


def _fix_packages(cve_ids: List[str], install_packages: Optional[List[str]]) -> Optional[List[str]]:
    """The packages a CVE fix installs: the plan's list, or Inspector's for the CVEs; None when unknown."""
    if install_packages:
        return list(install_packages)
    names = set()
    for finding in _shared._active_cve_findings(cve_ids):
        for vp in (finding.get('packageVulnerabilityDetails') or {}).get('vulnerablePackages') or []:
            if vp.get('name'):
                names.add(vp['name'])
    return sorted(names) or None


def _scheduled_window(environment: str, instance_ids: List[str], sla_hours, sla_source,
                      first_observed_at, cve_id, additional_cve_ids, reboot_option: Optional[str] = None,
                      install_packages: Optional[List[str]] = None) -> Tuple[Optional[dict], str, Optional[dict]]:
    """(The SCHEDULED result when a maintenance window or patch policy patches every target inside
    the SLA, else None; the SLA check's text for a plan that runs now; the coverage by location).

    See helper.coverage for what counts. Anything it can't prove means patch now."""
    # The SLA clock starts at discovery, so a window that opens inside
    # sla_hours from now can still land after the deadline.
    if sla_source == _shared.SLA_TAGS_UNREADABLE:
        return None, (f"Some targets' SLA tags couldn't be read, so their SLA may be stricter than {sla_hours} h "
                      "and Patchy can't show any window meets it; it runs now."), None
    cve_ids = [c for c in [cve_id] + (additional_cve_ids or []) if c]
    try:
        # The clock runs from when these targets were first seen exposed (2 Nov review, L2).
        observed_at = first_observed_at or _earliest_first_observed_at(cve_ids, instance_ids=instance_ids,
                                                                       strict=bool(cve_ids))
    except _shared.InspectorUnreadable:
        # Without the discovery time, "now" would push the deadline out and could let a
        # window qualify after the real deadline has passed (2 Nov review, H1).
        return None, (f"Inspector couldn't be read for when {', '.join(cve_ids)} was first seen, so Patchy can't "
                      f"show any window meets the {sla_hours} h SLA ({sla_source or 'default'}); it runs now."), None
    deadline = (_shared._coerce_utc(observed_at) or datetime.now(timezone.utc)) + timedelta(hours=int(sla_hours))
    reboots = (reboot_option or _reboot_option(_normalize_environment(environment or '') or 'prod')) == 'RebootIfNeeded'
    try:
        result = _coverage_check(environment, instance_ids, deadline, plan_reboots=reboots, cve_ids=cve_ids,
                                 packages=_fix_packages(cve_ids, install_packages) if cve_ids else None)
    except Exception as e:
        logger.warning(f"[SLA_WINDOW] coverage check failed: {e}")
        return None, (f"Patchy couldn't check the maintenance windows and patch policies before the "
                      f"{sla_hours} h SLA ({sla_source or 'default'}), so it runs now."), None
    coverage = {k: result[k] for k in ('rows', 'deadline')}
    if result['all_covered']:
        latest = result['latest'] or {}
        finishes = _shared._coerce_utc(latest.get('finishes_at'))
        hours = round(max(0.0, (finishes - datetime.now(timezone.utc)).total_seconds() / 3600), 1) if finishes else None
        return {
            "status": "schedule_recommended",
            "result_type": "gate_blocked",
            "decision": "SCHEDULED",
            "summary": (
                f"Every target is scheduled to be patched before the {sla_hours}h SLA deadline: "
                f"{'; '.join(sorted({r['schedule'] for r in result['rows']}))}. The last of them finishes "
                f"in about {hours}h. No action is needed unless you want to patch now."
            ),
            "sla_assessment": {
                "sla_hours": sla_hours,
                "sla_source": sla_source,
                "covering_schedules": sorted({r['schedule'] for r in result['rows']}),
                "last_scheduled_finish_utc": latest.get('finishes_at'),
                "hours_until_last_finish": hours,
                "sla_deadline_utc": result['deadline'],
                "window_within_sla": True,
            },
            "coverage": coverage,
            "options": [
                "Wait for the schedules -- reply 'wait' or 'defer' (no further action)",
                "Override and patch immediately -- reply 'patch now' or 'proceed' "
                "(the LLM will retry with force_emergency=True)",
            ],
            "instance_ids": instance_ids,
            "environment": environment,
            "category": "ABORT",
            "retryable": True,
            "error_code": "ScheduleRecommended",
            "next_action": "The operation was NOT started. Present this to the operator.",
        }, "", coverage
    if result['covered']:
        from helper.coverage import table_text
        return None, table_text(result, sla_hours, sla_source), coverage
    text = _window_check_text(sla_hours, sla_source, result['windows_skipped'])
    if not result.get('policies_read'):
        text = text[:-1] + " (Quick Setup patch policies couldn't be read, so none counted)."
    return None, text, coverage


DRY_RUN_MAX_AGE_HOURS = 2


def _scan_refusal(error: str, code: str, suggestion: Optional[str] = None) -> dict:
    return {"error": error, "error_code": code, "result_type": "error", "category": "ABORT",
            **({"suggestion": suggestion} if suggestion else {}), "retryable": False,
            "next_action": "The operation was NOT started. Present this to the operator."}


def _scan_check(scan_execution_id: str, instance_ids: Optional[List[str]] = None,
                environment: Optional[str] = None, account_ids: Optional[List[str]] = None,
                regions: Optional[List[str]] = None) -> Tuple[Optional[dict], Optional[dict]]:
    """(refusal, scan): whether the named scan is a successful, recent Patchy dry-run of these targets.

    Run when the plan is made and again when it runs (2 Nov review, M12). Refuses when
    the scan didn't succeed, is over DRY_RUN_MAX_AGE_HOURS old, isn't a Patchy scan, or
    scanned none of the targets, a different environment, or other accounts or Regions.
    A scan of some of the instances passes with scan['missed'] naming how many it missed.
    """
    from helper.retry import throttled
    try:
        scan_exec = throttled(lambda: _ssm().get_automation_execution(
            AutomationExecutionId=scan_execution_id))['AutomationExecution']
    except ClientError as e:
        return _scan_refusal(f"Cannot verify dry-run scan: {e}", "DryRunVerificationFailed"), None
    scan_status = scan_exec.get('AutomationExecutionStatus')
    if scan_status != 'Success':
        return _scan_refusal(f"Dry-run scan {scan_execution_id} status: {scan_status}. Must be Success.",
                             "DryRunNotComplete"), None
    exec_time = scan_exec.get('ExecutionStartTime')
    if exec_time:
        if exec_time.tzinfo is None:
            exec_time = exec_time.replace(tzinfo=timezone.utc)
        hours_since = (datetime.now(timezone.utc) - exec_time).total_seconds() / 3600
        if hours_since > DRY_RUN_MAX_AGE_HOURS:
            return _scan_refusal(f"Scan {scan_execution_id} is {hours_since:.1f}hr old.", "DryRunStale",
                                 "Run a fresh patch_dry_run."), None
    params = scan_exec.get('Parameters') or {}
    doc = (scan_exec.get('DocumentName') or '').split('/')[-1]
    not_this = f"Scan {scan_execution_id} isn't a dry-run of this plan's targets"
    if (params.get('Operation') or [None])[0] != 'Scan' or doc not in (
            _shared.AUTOMATION_DOC_NAME, _shared.AUTOMATION_BY_ID_DOC_NAME):
        return _scan_refusal(f"{not_this}: it ran {doc or 'an unknown document'}"
                             f" ({(params.get('Operation') or ['no operation'])[0]}), not a Patchy scan.",
                             "DryRunMismatch", "Run patch_dry_run on these targets, or plan without a scan."), None
    scan = {'execution_id': scan_execution_id, 'at': exec_time.isoformat() if exec_time else None}
    by_tag = doc == _shared.AUTOMATION_DOC_NAME
    if instance_ids:
        if by_tag:
            # An environment scan's record doesn't list its instances, so coverage can't be confirmed.
            scan['by_tag'] = _normalize_environment((params.get('Environment') or [''])[0])
            return None, scan
        scanned = {v for t in scan_exec.get('Targets') or [] if t.get('Key') == 'ParameterValues'
                   for v in t.get('Values') or []}
        missed = [i for i in instance_ids if i not in scanned]
        if len(missed) == len(instance_ids):
            return _scan_refusal(f"{not_this}: it scanned none of them.", "DryRunMismatch",
                                 "Run patch_dry_run on these targets, or plan without a scan."), None
        if missed:
            scan['missed'] = len(missed)
        return None, scan
    if environment:
        if not by_tag:
            return _scan_refusal(f"{not_this}: it scanned named instances, not the environment.",
                                 "DryRunMismatch",
                                 "Run multi_account_dry_run on this environment, or plan without a scan."), None
        scan_env = _normalize_environment((params.get('Environment') or [''])[0])
        locations = scan_exec.get('TargetLocations') or []
        scan_accounts = {a for loc in locations for a in loc.get('Accounts') or []}
        scan_regions = {r for loc in locations for r in loc.get('Regions') or []}
        other = (f"the {scan_env or 'unknown'} environment" if scan_env != _normalize_environment(environment)
                 else "other accounts" if not set(account_ids or []) <= scan_accounts
                 else "other Regions" if not set(regions or []) <= scan_regions else None)
        if other:
            return _scan_refusal(f"{not_this}: it scanned {other}.", "DryRunMismatch",
                                 "Run multi_account_dry_run on this environment, or plan without a scan."), None
    return None, scan


def _dry_run_gate(scan_execution_id: Optional[str], **targets) -> Optional[dict]:
    """A refusal when a scan ran in this same request, or the named scan didn't succeed, is stale
    or isn't a dry-run of these targets."""
    # -- Operator confirmation gate: block if dry-run ran in this same request --
    scans = _current_request_scans.get()
    if scans is not None and len(scans) > 0:
        logger.warning(f"[DRY-RUN GATE] Blocked: dry-run scan ran in the same request (scans={scans})")
        return {
            "error": "A dry-run scan was just run in this request. The operator must review the results before patching can proceed.",
            "error_code": "OperatorConfirmationRequired",
            "result_type": "gate_blocked",
            "category": "ABORT",
            "suggestion": "Present the dry-run results to the operator. When they confirm, call execute_patch_operation in the next message.",
            "retryable": False,
            "next_action": "The operation was NOT started. Present this to the operator.",
        }

    # -- Dry-run gate: verify scan if provided (optional) --
    if scan_execution_id:
        return _scan_check(scan_execution_id, **targets)[0]
    return None


def _start_by_location(groups: Dict[Tuple[str, str], List[str]], start, tool: str
                       ) -> Tuple[List[Tuple[str, str, str, List[str]]], List[Dict[str, Any]]]:
    """Start one run per (account, region) with start(account, region, instance_ids).

    Returns (started as (account, region, execution ID, instance IDs), not started as
    {account, region, instance_ids, error}). After the first failure nothing more is started.
    A failure before anything started is raised, as a plain error with nothing to record.
    """
    started: List[Tuple[str, str, str, List[str]]] = []
    not_started: List[Dict[str, Any]] = []
    items = list(groups.items())
    for i, ((account, region), iids) in enumerate(items):
        try:
            exec_id = start(account, region, iids)
        except Exception as e:
            if not started:
                raise
            logger.error(f"[TOOL:{tool}] start failed in {account}/{region} after {len(started)} started: {e}")
            not_started.append({'account': account, 'region': region, 'instance_ids': iids, 'error': str(e)[:300]})
            not_started += [{'account': a, 'region': r, 'instance_ids': g,
                             'error': 'not attempted after an earlier location failed'} for (a, r), g in items[i + 1:]]
            break
        started.append((account, region, exec_id, iids))
        logger.info(f"[TOOL:{tool}] Automation started: account={account} "
                    f"region={region} instances={len(iids)} exec_id={exec_id}")
    return started, not_started


def _partial_start(execution_ids: List[str], not_started: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Result fields for a run that started in some locations only."""
    where = "; ".join(f"{n['account']}/{n['region']}: {n['error']}" for n in not_started)
    return {
        'status': 'PARTIALLY_STARTED',
        'not_started': not_started,
        'next_action': (f"Partly started. Give execution IDs {', '.join(execution_ids)} exactly and say these "
                        f"locations didn't start: {where}. The plan can't be run again; the operator makes a "
                        "new plan for them. Then stop."),
    }


def _start_patch_runs(instance_ids: List[str], environment: str, severity_filter: Optional[str],
                      install_override_url: Optional[str], reboot_option: Optional[str]
                      ) -> Tuple[List[str], List[str], List[str], List[str], List[Dict[str, Any]], Dict[str, tuple]]:
    """Start one instance-ID Automation per (account, region).

    Returns (execution IDs, accounts, regions, started instance IDs, locations not started,
    and each execution's own (account, region, instance IDs)).
    """
    limits = run_limits(environment)

    def start(account, region, group_iids):
        return _start_instance_patch_automation(
            operation='Install',
            instance_ids=group_iids,
            account_ids=[account],
            regions=[region],
            max_concurrency=limits['account_max_concurrency'],
            max_errors=limits['account_max_errors'],
            severity_filter=severity_filter,
            environment=environment,
            install_override_url=install_override_url,
            reboot_option=reboot_option or _reboot_option(environment),
        )
    started, not_started = _start_by_location(_group_instances_by_location(instance_ids), start,
                                              'execute_patch_operation')
    started_iids = {i for _, _, _, iids in started for i in iids}
    return ([e for _, _, e, _ in started], sorted({a for a, _, _, _ in started}),
            sorted({r for _, r, _, _ in started}), [i for i in instance_ids if i in started_iids], not_started,
            {e: (a, r, list(iids)) for a, r, e, iids in started})


def _execute_patch_operation_impl(
    instance_ids: List[str],
    execution_mode: str,
    environment: str,
    severity_filter: Optional[str] = None,
    scan_execution_id: Optional[str] = None,
    confirm_execute: bool = False,
    confirm_no_scan: bool = False,
    force_emergency: bool = False,
    cve_id: Optional[str] = None,
    severity: Optional[str] = None,
    cvss_score: Optional[float] = None,
    additional_cve_ids: Optional[List[str]] = None,
    sla_hours: Optional[int] = None,
    sla_source: Optional[str] = None,
    first_observed_at: Optional[str] = None,
    pre_patch_state: Optional[Dict[str, Any]] = None,
    install_override_url: Optional[str] = None,
    install_packages: Optional[List[str]] = None,
    reboot_option: Optional[str] = None,
) -> dict:
    """Execute patching via SSM Automation (MAMR). Always uses instance-ID targeting.

    Decision: Use when operator names specific instance IDs (i-xxx) in their message.
    For fleet-scope patching by environment/severity/CVE, use multi_account_execute instead.

    Always derives sla_hours and sla_source from instance tags when not passed
    explicitly -- the audit trail must record the SLA the patch ran against,
    regardless of whether the operator's verb was "patch" or "emergency."

    Always checks the next maintenance window. If a window opens within the
    SLA deadline AND force_emergency=False, returns a SCHEDULED recommendation
    so the operator can defer to the existing patch policy. Operator approval
    flips force_emergency=True for the retry.

    Pass scan_execution_id if a prior dry-run was run (optional -- verifies scan succeeded).
    If confirm_execute is False, returns a confirmation request showing the execution plan.
    If no scan_execution_id and confirm_no_scan is False, returns a warning asking operator to confirm patching without a preview.

    Args:
        instance_ids: EC2 instance IDs to patch
        execution_mode: "immediate" only. Cross-region/cross-account scheduled
                        patching is delegated to the operator's existing SSM
                        Patch Policy via the SCHEDULED recommendation flow,
                        not by passing "scheduled" here.
        environment: dev, staging, or prod
        severity_filter: CRITICAL, HIGH, MEDIUM, or LOW -- scopes to that level and above
        scan_execution_id: Automation execution ID from patch_dry_run result
        force_emergency: True after the operator has reviewed the SCHEDULED
                         recommendation (or explicitly used emergency phrasing)
                         and chose to patch immediately. Suppresses the
                         schedule-vs-window comparison; SLA is still recorded.
        cve_id: Primary CVE being remediated (forwarded to compliance report)
        severity: CVE severity. Used to derive sla_hours from tags when not passed.
        cvss_score: CVSS score (forwarded to compliance report)
        additional_cve_ids: Other CVEs fixed in same operation
        sla_hours: SLA deadline override. If not passed, derived from instance tags.
        sla_source: SLA source override. If not passed, derived from instance tags.
        pre_patch_state: From capture_patch_state (forwarded for before/after delta)

    Returns: {status, automation_execution_id, instance_count, environment}
    """
    try:
        # Validate instance IDs and scope tag
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error
        scope_error = _validate_instance_scope(instance_ids, fresh=confirm_execute)
        if scope_error:
            return scope_error

        # The environment comes from each instance's own tag; a request can't
        # file a prod instance under dev, or mix environments in one plan.
        requested_env = _normalize_environment(environment)
        tagged_envs = _instance_environments(instance_ids)
        mismatched = {iid: env for iid, env in tagged_envs.items() if env and env != requested_env}
        if mismatched:
            return {
                "error": (f"Instance environment doesn't match the request ('{requested_env}'): "
                          + ", ".join(f"{iid} is tagged {env}" for iid, env in mismatched.items())),
                "error_code": "EnvironmentMismatch",
                "result_type": "gate_blocked",
                "category": "ABORT",
                "retryable": False,
                "instance_environments": tagged_envs,
                "next_action": "Nothing was planned. Tell the operator which instances belong to which "
                               "environment and plan each environment separately.",
            }
        untagged_env = [iid for iid, env in tagged_envs.items() if env is None]
        # Untagged instances count as prod (as rollback already does), so they
        # get prod's limits, reboot policy and promotion check.
        if untagged_env and requested_env != 'prod':
            return {
                "error": (f"No Environment tag on {', '.join(untagged_env[:5])}; untagged instances count as prod, "
                          f"not {requested_env}."),
                "error_code": "UntaggedCountsAsProd",
                "result_type": "gate_blocked",
                "category": "ABORT",
                "retryable": False,
                "untagged_instances": untagged_env,
                "next_action": "Nothing was planned. Tell the operator to tag the instances, or plan them as prod.",
            }
        environment = requested_env

        logger.info(f"[TOOL:execute_patch_operation] mode={execution_mode} env={environment} "
                     f"instances={len(instance_ids)} severity_filter={severity_filter} "
                     f"scan_id={scan_execution_id} force_emergency={force_emergency} "
                     f"instance_ids={instance_ids[:5]}")

        # A named CVE's severity comes from Inspector, not the model; if Inspector can't
        # be read, the model's value would set the SLA, so the plan stops instead.
        if cve_id:
            try:
                severity = _cve_severity([cve_id] + (additional_cve_ids or [])) or severity
            except _shared.InspectorUnreadable as e:
                return _inspector_unreadable(cve_id, e)

        # Always derive SLA from instance tags so the audit trail is complete
        # regardless of whether the agent forwarded sla_hours explicitly.
        # Operator-supplied values take precedence; otherwise read tags.
        derived_frameworks: List[str] = []
        if (sla_hours is None or sla_source is None) and severity:
            resolved = _resolve_sla_for_instances(instance_ids, severity)
            if sla_hours is None:
                sla_hours = resolved.get('sla_hours')
            if sla_source is None:
                sla_source = resolved.get('sla_source')
            derived_frameworks = resolved.get('frameworks') or []
        elif severity:
            # Even when sla_hours/source were passed, look up frameworks for
            # the audit trail. Cheap when cache is warm.
            resolved = _resolve_sla_for_instances(instance_ids, severity)
            derived_frameworks = resolved.get('frameworks') or []

        # SLA-vs-window decision. Strict -- runs for both Path A and Path B,
        # regardless of how the operator phrased the request, so we never
        # default to emergency when an existing maintenance window would
        # cover the SLA. The operator can override via force_emergency=True.
        window_check, coverage = "", None
        if not sla_hours and severity_filter:
            # What the model reads, so it doesn't call this an emergency for want of a window.
            window_check = _SEVERITY_RUNS_NOW
        if not force_emergency and not confirm_execute and sla_hours:
            scheduled, window_check, coverage = _scheduled_window(
                environment, instance_ids, sla_hours, sla_source, first_observed_at, cve_id, additional_cve_ids,
                reboot_option=reboot_option, install_packages=install_packages)
            if scheduled:
                return scheduled

        refused = _location_limit(environment, _group_instances_by_location(instance_ids), 'execute_patch_operation')
        if refused:
            return refused

        # Gate: operator must confirm execution intent
        if not confirm_execute:
            why_now = _why_now(_run_now_reason(force_emergency, sla_hours, severity_filter, coverage))
            return {
                "status": "confirmation_required",
                "result_type": "gate_blocked",
                "warning": (
                    f"About to patch {len(instance_ids)} instance(s) in environment "
                    f"'{environment}'. SLA: {sla_hours}h ({sla_source or 'default'}). "
                    f"Runs now, once approved: {why_now}."
                ),
                "execution_plan": {
                    "instance_ids": instance_ids,
                    "environment": environment,
                    "untagged_environment_instances": untagged_env,
                    "execution_mode": execution_mode,
                    "severity_filter": severity_filter,
                    "scan_execution_id": scan_execution_id,
                    "sla_hours": sla_hours,
                    "sla_source": sla_source,
                    "window_check": window_check,
                    "coverage": coverage,
                    "decision": "EMERGENCY",
                    "run_now_reason": _run_now_reason(force_emergency, sla_hours, severity_filter, coverage),
                    "why_now": why_now,
                },
                "question": "Confirm to proceed with patching, or cancel.",
                "error_code": "ExecutionConfirmation",
                "category": "ABORT",
                "retryable": True,
                "next_action": "Preview only: the plan tool stores this as a plan for the operator to approve; nothing runs until then.",
            }

        # Note: no-scan advisory removed. The confirmation plan (Gate 1b above)
        # already shows the full scope. If no scan was done, the operator accepted
        # that when they confirmed. One confirmation is sufficient.

        refused = _dry_run_gate(scan_execution_id, instance_ids=instance_ids)
        if refused:
            return refused

        # Severity override validation
        if severity_filter and not _get_baseline_override_url(severity_filter):
            return {
                "status": "warning",
                "result_type": "error",
                "warning": f"Cannot execute severity-scoped patching: baseline override "
                           f"file for '{severity_filter}' is missing from S3.",
                "suggestion": "Run setup_baseline_overrides.py, then retry.",
                "severity_filter_requested": severity_filter,
                "next_action": "The operation was NOT started. Present this to the operator.",
            }

        if execution_mode == "scheduled":
            return {
                "error": "Scheduled mode is not supported for cross-region/cross-account patching. "
                         "Use execution_mode='immediate' for MAMR-based execution.",
                "error_code": "ScheduledNotSupported",
                "result_type": "error",
                "category": "ABORT",
                "suggestion": "Use execution_mode='immediate'. Scheduled cross-region patching is a V2 feature.",
                "retryable": False,
                "next_action": "The operation was NOT started. Present this to the operator.",
            }

        # -- Execute via MAMR (instance-ID based) --
        # Group instances by their owning (account, region) so each Automation
        # only targets the instances that actually live in that location.
        # Without this, a single Automation fans out to all (account, region)
        # children and each child receives every instance ID -- causing patches
        # against the wrong accounts to silently fail.
        operator = get_operator()
        # Every lookup happens before the start, so nothing that can fail sits
        # between starting a run and recording it.
        if first_observed_at is None:
            first_observed_at = _earliest_first_observed_at(
                [cve_id] + (additional_cve_ids or []), instance_ids=instance_ids)
        run_now_reason = _run_now_reason(force_emergency, sla_hours, severity_filter, None, checked=False)
        execution_ids, account_ids, target_regions, started_iids, not_started, shares = _start_patch_runs(
            instance_ids, environment, severity_filter, install_override_url, reboot_option)
        if not_started:
            # Only what started is recorded and reported; the rest needs a new plan.
            instance_ids = started_iids

        # Backwards-compat: surface first execution as `automation_execution_id`,
        # the full list as `automation_execution_ids`.
        execution_id = execution_ids[0] if execution_ids else ''

        # Write pending compliance context for later reconciliation.
        # One pending file per execution so the UI reconciler can correlate
        # status of each (account, region) child to its own report.
        # Each report covers its own execution's share, so one plan over two locations doesn't
        # report every instance twice (disruption round, 2 Nov).
        for eid in execution_ids:
            share_account, share_region, share_iids = shares.get(eid) or (None, None, instance_ids)
            _write_pending_compliance_context(eid, {
                'operation_type': 'patch',
                'targeting': 'instance-id',
                'environment': environment,
                'instance_ids': share_iids,
                'account_ids': [share_account] if share_account else account_ids,
                'regions': [share_region] if share_region else target_regions,
                'severity_filter': severity_filter,
                'install_packages': install_packages or [],
                'reboot_option': reboot_option or _reboot_option(environment),
                'execution_mode': execution_mode,
                'scan_execution_id': scan_execution_id,
                'decision': 'EMERGENCY' if execution_mode == 'immediate' else 'SCHEDULED',
                'decision_reason': (run_now_reason if execution_mode == 'immediate' else None),
                'cve_id': cve_id,
                'severity': severity,
                'cvss_score': cvss_score,
                'additional_cve_ids': additional_cve_ids,
                'sla_hours': sla_hours,
                'sla_source': sla_source,
                'first_observed_at': first_observed_at,
                'frameworks': derived_frameworks,
                'pre_patch_state': _unwrap_patch_state(pre_patch_state),
                'sibling_execution_ids': [x for x in execution_ids if x != eid],
            })

        logger.info(f"PATCH_EXECUTED: operator={operator} environment={environment} "
                    f"mode=immediate instances={len(instance_ids)} "
                    f"executions={len(execution_ids)} execution_ids={execution_ids}")

        return {
            'execution_mode': 'immediate',
            'status': 'EXECUTING',
            'result_type': 'execution_started',
            'automation_execution_id': execution_id,
            'automation_execution_ids': execution_ids,
            'instance_count': len(instance_ids),
            'instance_ids': instance_ids,
            'account_ids': account_ids,
            'regions': target_regions,
            'operator': operator,
            'environment': environment,
            'severity_filter': severity_filter,
            'compliance_report_required': True,
            'operation': 'patch',
            'estimated_duration': '3-5 minutes',
            'next_action': f"Started. Say what started and give execution ID {execution_id} exactly; it runs in the background and the operator can ask for status. Then stop.",
            **(_partial_start(execution_ids, not_started) if not_started else {}),
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"Error executing patch operation: {error_info}")
        error_info["instance_ids"] = instance_ids
        error_info["execution_mode"] = execution_mode
        return error_info
    except Exception as e:
        logger.error(f"Error executing patch operation: {e}")
        return classify_error(e)


@tool
def get_command_status(command_id: str, instance_ids: List[str]) -> dict:
    """Status of an SSM Run Command by command ID.

    For patch, scan and rollback runs use get_automation_status with the execution ID.

    Args:
        command_id: SSM command ID.
        instance_ids: Instances the command was sent to.
    """
    try:
        from concurrent.futures import as_completed
        from helper.concurrency import ContextThreadPoolExecutor as ThreadPoolExecutor
        logger.info(f"[TOOL:get_command_status] command_id={command_id} instances={len(instance_ids)}")

        # Validate instance IDs
        if instance_ids:
            validation_error = _validate_instance_ids(instance_ids)
            if validation_error:
                return validation_error

        # Handle empty instance list
        if not instance_ids:
            return {
                'command_id': command_id,
                'status': 'Unknown',
                'results': [],
                'summary': {'success': 0, 'failed': 0, 'in_progress': 0}
            }

        def get_instance_status(instance_id):
            """Helper to get status for single instance"""
            try:
                response = _ssm().get_command_invocation(
                    CommandId=command_id,
                    InstanceId=instance_id
                )
                return {
                    'instance_id': instance_id,
                    'status': response['Status'],
                    'output': response.get('StandardOutputContent', '')[:500]
                }
            except Exception as e:
                return {
                    'instance_id': instance_id,
                    'status': 'Unknown',
                    'output': str(e)
                }

        # Parallel execution with max 10 threads
        results = []
        with ThreadPoolExecutor(max_workers=min(_COMMAND_STATUS_WORKERS, len(instance_ids))) as executor:
            futures = {executor.submit(get_instance_status, iid): iid for iid in instance_ids}
            for future in as_completed(futures):
                iid = futures[future]
                try:
                    results.append(future.result())
                except Exception as e:
                    logger.warning(f"[TOOL:get_command_status] Worker failed for {iid}: {e}")
                    results.append({'instance_id': iid, 'status': 'Unknown', 'output': str(e)})

        # Calculate summary
        summary = {
            'success': len([r for r in results if r['status'] == 'Success']),
            'failed': len([r for r in results if r['status'] == 'Failed']),
            'in_progress': len([r for r in results if r['status'] == 'InProgress'])
        }

        # Overall status
        if summary['in_progress'] > 0:
            overall_status = 'InProgress'
        elif summary['failed'] > 0:
            overall_status = 'Failed'
        else:
            overall_status = 'Success'

        # Check for pending reboots after successful patching
        reboot_info = None
        if overall_status == 'Success':
            try:
                patch_states = _ssm().describe_instance_patch_states(InstanceIds=instance_ids)
                pending = [
                    {'instance_id': s['InstanceId'], 'pending_reboot_count': s.get('InstalledPendingRebootCount', 0)}
                    for s in patch_states.get('InstancePatchStates', [])
                    if s.get('InstalledPendingRebootCount', 0) > 0
                ]
                if pending:
                    reboot_info = {
                        'reboot_required': True,
                        'instances_pending_reboot': len(pending),
                        'details': pending,
                        'warning': f"{len(pending)} instance(s) require reboot for patches to take effect. "
                                   "Kernel and security patches remain inactive until reboot. "
                                   "Plan a reboot during your next maintenance window."
                    }
            except Exception as e:
                logger.warning(f"Could not check reboot status: {e}")

        response = {
            'command_id': command_id,
            'status': overall_status,
            'results': results,
            'summary': summary
        }
        if reboot_info:
            response['reboot'] = reboot_info
        return response
    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"Error getting command status: {error_info}")
        error_info["command_id"] = command_id
        return error_info
    except Exception as e:
        logger.error(f"Error getting command status: {e}")
        return classify_error(e)


def _rollback_patches_impl(instance_ids: List[str], confirm_execute: bool = False,
                           since: Optional[int] = None, until: Optional[int] = None) -> dict:
    """Rollback patches on EC2 instances via SSM Automation (MAMR).

    Decision: Use when operator names specific instance IDs for rollback.
    For fleet-scope rollback, use multi_account_rollback instead.

    Uses the Patchy-RunRollbackById Automation document with instance-ID
    targeting. It undoes the yum transactions that began between since and
    until: the window of the last recorded Patchy patch run.

    With confirm_execute=False it returns a preview, which the rollback_patches
    plan tool stores as a plan; the runtime runs an approved plan with
    confirm_execute=True.

    Args:
        instance_ids: List of EC2 instance IDs to rollback patches on
        confirm_execute: True to skip the confirmation gate. Default False
                         returns the plan instead of executing.

    Returns:
        dict: {'automation_execution_id': str, 'status': str, 'instance_count': int}
    """
    try:
        # Validate instance IDs and scope tag
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error
        scope_error = _validate_instance_scope(instance_ids, fresh=confirm_execute)
        if scope_error:
            return scope_error

        # Strictest environment among the instances; untagged counts as prod.
        rank = {'dev': 0, 'staging': 1}
        envs = _instance_environments(instance_ids).values()
        rollback_env = max(envs, key=lambda e: rank.get(e, 2), default='prod') or 'prod'
        groups = _group_instances_by_location(instance_ids)
        refused = _location_limit(rollback_env, groups, 'rollback_patches')
        if refused:
            return refused

        # Gate: operator must confirm rollback intent. Rollback is destructive
        # (yum history undo on each instance) and warrants the same explicit
        # gate as install operations.
        if not confirm_execute:
            return {
                "status": "confirmation_required",
                "result_type": "gate_blocked",
                "warning": f"About to roll back patches on {len(instance_ids)} instance(s). "
                           f"This undoes the yum transactions from the last Patchy patch run on each instance.",
                "execution_plan": {
                    "instance_ids": instance_ids,
                    "operation": "rollback",
                },
                "question": "Confirm to proceed with rollback, or cancel.",
                "error_code": "ExecutionConfirmation",
                "category": "ABORT",
                "retryable": True,
                "next_action": "Preview only: the plan tool stores this as a plan for the operator to approve; nothing runs until then.",
            }

        logger.info(f"[TOOL:rollback_patches] instances={len(instance_ids)} instance_ids={instance_ids[:5]}")

        rollback_limits = run_limits(rollback_env)

        def start(account, region, group_iids):
            return _start_instance_rollback_automation(
                instance_ids=group_iids,
                account_ids=[account],
                regions=[region],
                max_concurrency=rollback_limits['account_max_concurrency'],
                max_errors=rollback_limits['account_max_errors'],
                environment=rollback_env,
                since=since,
                until=until,
            )
        started, not_started = _start_by_location(groups, start, 'rollback_patches')
        execution_ids = [e for _, _, e, _ in started]
        executed_account_ids = [a for a, _, _, _ in started]
        executed_regions = [r for _, r, _, _ in started]
        if not_started:
            # Only what started is recorded and reported; the rest needs a new plan.
            started_iids = {i for _, _, _, iids in started for i in iids}
            instance_ids = [i for i in instance_ids if i in started_iids]

        execution_id = execution_ids[0] if execution_ids else ''
        account_ids = sorted(set(executed_account_ids))
        target_regions = sorted(set(executed_regions))

        # Write pending compliance context for rollback reconciliation
        shares = {e: (a, r, list(iids)) for a, r, e, iids in started}
        for eid in execution_ids:
            share_account, share_region, share_iids = shares[eid]
            _write_pending_compliance_context(eid, {
                'operation_type': 'rollback',
                'targeting': 'instance-id',
                'environment': 'unknown',
                'instance_ids': share_iids,
                'account_ids': [share_account],
                'regions': [share_region],
            })

        logger.info(f"[TOOL:rollback_patches] INITIATED: executions={len(execution_ids)} "
                    f"execution_ids={execution_ids} instances={len(instance_ids)}")

        return {
            'status': 'ROLLBACK_INITIATED',
            'result_type': 'execution_started',
            'automation_execution_id': execution_id,
            'automation_execution_ids': execution_ids,
            'instance_count': len(instance_ids),
            'instance_ids': instance_ids,
            'account_ids': account_ids,
            'regions': target_regions,
            'operation': 'rollback',
            'estimated_duration': '3-5 minutes',
            'next_action': f"Started. Say what started and give execution ID {execution_id} exactly; it runs in the background and the operator can ask for status. Then stop.",
            **(_partial_start(execution_ids, not_started) if not_started else {}),
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"Error initiating rollback: {error_info}")
        error_info["instance_ids"] = instance_ids
        error_info["suggestion"] = (
            f"{error_info.get('suggestion', '')} "
            "CRITICAL: Rollback failed. Manual intervention required. "
            "Check SSM connectivity and instance state."
        ).strip()
        return error_info
    except Exception as e:
        logger.error(f"Error initiating rollback: {e}")
        result = classify_error(e)
        result["instance_ids"] = instance_ids
        result["suggestion"] = (
            "CRITICAL: Rollback failed. Manual intervention required. "
            "Verify SSM connectivity and instance state."
        )
        return result


_RUN_ACTIVE = ('Pending', 'InProgress', 'Waiting', 'Cancelling', 'ChangeCalendarOverrideApproved',
               'PendingChangeCalendarOverride', 'PendingApproval', 'Approved', 'Scheduled', 'RunbookInProgress')
_RUN_SUCCEEDED = ('Success', 'CompletedWithSuccess')
_RUN_FAILED_CHILD = ('Failed', 'TimedOut', 'CompletedWithFailure', 'Rejected')


def _run_outcome(automation_execution_id: str):
    """Read an Automation run once.

    Returns:
        (state, run): state is RUNNING, SUCCEEDED, PARTIAL, FAILED or ERROR; run is
        the get_automation_status result.
    """
    run = get_automation_status(automation_execution_id)
    if 'error' in run or 'status' not in run:
        return 'ERROR', run
    if run['status'] in _RUN_ACTIVE or run.get('children_in_progress'):
        return 'RUNNING', run
    if run['status'] not in _RUN_SUCCEEDED:
        return 'FAILED', run
    if run.get('children_failed') or run.get('children_cancelled'):
        return 'PARTIAL', run
    if run.get('children_unreadable'):
        return 'ERROR', run  # the parent says done, but its locations couldn't be read
    return 'SUCCEEDED', run


def _run_summary(run: dict) -> dict:
    return {k: run.get(k) for k in ('automation_execution_id', 'status', 'children_total',
                                    'children_succeeded', 'children_failed', 'children_cancelled',
                                    'failure_message')}


@tool
def verify_rollback(automation_execution_id: str, instance_ids: List[str],
                    pre_patch_state: Optional[Dict[str, Any]] = None,
                    post_patch_state: Optional[Dict[str, Any]] = None,
                    scan_execution_id: Optional[str] = None) -> dict:
    """Check that a rollback run finished and the patches were reverted.

    Use after an approved rollback when the operator asks whether it worked. Returns WAIT
    at once if the run is still going. With a snapshot, the first call starts a scan and
    returns WAIT with scan_execution_id; call again with it once the scan has finished
    (2 to 3 minutes) to compare patch counts.

    Args:
        automation_execution_id: Execution ID from the rollback run card.
        instance_ids: Instances that were rolled back.
        pre_patch_state: capture_patch_state output from before patching, if any.
        post_patch_state: capture_patch_state output from after patching (more precise).
        scan_execution_id: The scan this tool started on an earlier call.
    """
    # Non-blocking: one status read. The re-scan, when snapshots are given, runs
    # through patch_dry_run so it reaches instances in every account and region.
    try:
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error
        scope_error = _validate_instance_scope(instance_ids)
        if scope_error:
            return scope_error

        logger.info(f"[TOOL:verify_rollback] execution={automation_execution_id} instances={len(instance_ids)} "
                     f"has_pre_patch={pre_patch_state is not None} has_post_patch={post_patch_state is not None}")

        state, run = _run_outcome(automation_execution_id)
        if state == 'ERROR':
            run['recommendation'] = 'INVESTIGATE'
            return run
        if state == 'RUNNING':
            return {'status': 'IN_PROGRESS', 'recommendation': 'WAIT', 'run': _run_summary(run),
                    'reason': f"Rollback run is {run['status']}. Nothing to verify yet; check again in a few minutes."}
        if state == 'FAILED':
            return {'status': 'FAILED', 'recommendation': 'MANUAL_INTERVENTION', 'run': _run_summary(run),
                    'reason': f"Rollback run ended {run['status']}."}

        patch_state_comparison = {}
        pre_patch_state = _unwrap_patch_state(pre_patch_state)
        post_patch_state = _unwrap_patch_state(post_patch_state)
        compare = bool(pre_patch_state or post_patch_state)
        # With a snapshot, only the comparison confirms the rollback; when it couldn't
        # run, the rollback isn't verified (2 Nov review, M5).
        unverified: List[str] = []
        if compare and not scan_execution_id:
            # The patch state SSM holds is from before the rollback until a scan runs.
            scan = patch_dry_run(instance_ids)
            if not scan.get('error'):
                scan_id = scan.get('automation_execution_id')
                return {'status': 'SCAN_STARTED', 'recommendation': 'WAIT', 'run': _run_summary(run),
                        'scan_execution_id': scan_id,
                        'reason': "The rollback finished. A scan has started so the patch state reflects it.",
                        'next_action': (f"Say the rollback finished and a scan ({scan_id}) is reading the new patch "
                                        "state. In 2 to 3 minutes, call verify_rollback again with the same "
                                        f"arguments and scan_execution_id={scan_id}.")}
            logger.warning(f"[TOOL:verify_rollback] Post-rollback scan failed: {scan.get('error')}")
            unverified.append(f"The scan that reads the new patch state didn't start ({scan.get('error')}), "
                              "so the patch state wasn't compared")
            compare = False
        if compare and scan_execution_id:
            scan_state, _ = _run_outcome(scan_execution_id)
            if scan_state == 'RUNNING':
                return {'status': 'SCAN_IN_PROGRESS', 'recommendation': 'WAIT', 'run': _run_summary(run),
                        'scan_execution_id': scan_execution_id,
                        'reason': "The scan that reads the new patch state is still running; check again shortly."}
            if scan_state in ('FAILED', 'ERROR'):
                logger.warning(f"[TOOL:verify_rollback] scan {scan_execution_id} ended {scan_state}; no comparison")
                unverified.append(f"The scan {scan_execution_id} ended {scan_state}, so the patch state wasn't compared")
                compare = False
        if compare:
            try:
                from .compliance_tools import capture_patch_state
                current_map = (capture_patch_state(instance_ids) or {}).get('snapshot') or {}

                for iid in instance_ids:
                    after_raw = current_map.get(iid, {})
                    if not after_raw or after_raw.get('no_data'):
                        patch_state_comparison[iid] = {
                            'comparison_basis': 'no_data', 'reverted': None,
                            'note': "No patch state for this instance after the rollback, so it can't be compared."}
                        continue
                    after = {
                        'missing_count': after_raw.get('missing_count', 0),
                        'installed_count': after_raw.get('installed_count', 0),
                    }
                    if post_patch_state and iid in post_patch_state:
                        # Preferred: compare against post-patch state
                        # After rollback, missing_count should increase from post-patch (0 or low)
                        # because reverted packages move back to Missing
                        post = post_patch_state[iid]
                        post_missing = post.get('missing_count', 0)
                        post_installed = post.get('installed_count', 0)
                        after_missing = after['missing_count']
                        after_installed = after['installed_count']

                        # Rollback verified if: missing went up OR installed went down from post-patch
                        reverted = (after_missing > post_missing) or (after_installed < post_installed)

                        patch_state_comparison[iid] = {
                            'comparison_basis': 'post_patch',
                            'post_patch_missing': post_missing,
                            'post_patch_installed': post_installed,
                            'after_rollback_missing': after_missing,
                            'after_rollback_installed': after_installed,
                            'reverted': reverted,
                        }
                    elif pre_patch_state and iid in pre_patch_state:
                        # Fallback: compare against pre-patch state
                        # NOTE: This comparison is less reliable because SSM reconciles
                        # compliance tracking during Install (packages already at latest
                        # version get marked Installed without yum changing them).
                        # We check if missing_count is within a reasonable range -- not
                        # exact match, but at least some patches moved back to Missing.
                        before = pre_patch_state[iid]
                        before_missing = before.get('missing_count', 0)
                        after_missing = after['missing_count']

                        # Rollback shows some effect if missing_count > 0 (something reverted)
                        # We can't reliably expect exact match due to SSM reconciliation
                        reverted = after_missing > 0 if before_missing > 0 else True

                        patch_state_comparison[iid] = {
                            'comparison_basis': 'pre_patch_fallback',
                            'before_missing': before_missing,
                            'after_missing': after_missing,
                            'before_installed': before.get('installed_count', 0),
                            'after_installed': after['installed_count'],
                            'reverted': reverted,
                            'note': 'Pre-patch comparison is approximate due to SSM compliance reconciliation. Pass post_patch_state for precise verification.'
                        }

            except Exception as e:
                logger.warning(f"[TOOL:verify_rollback] Post-rollback scan failed: {e}")
                unverified.append(f"The patch state after the rollback couldn't be compared ({e})")
                compare = False

        # Log comparison results
        for iid, comp in patch_state_comparison.items():
            logger.info(f"[TOOL:verify_rollback] COMPARISON {iid}: basis={comp.get('comparison_basis','none')} reverted={comp.get('reverted','N/A')}")

        # Health checks
        from .maintenance_tools import check_instance_health, check_cloudwatch_alarms
        ssm_health = check_instance_health(instance_ids)
        alarm_health = check_cloudwatch_alarms(instance_ids)

        # Build per-instance results. The run reports per account and region, so
        # an instance's run status is the run's overall result.
        cmd_status = 'Success' if state == 'SUCCEEDED' else 'Partial'
        instance_results = []
        if state != 'SUCCEEDED':
            unverified.append(f"The rollback run ended {run.get('status')} with {run.get('children_failed')} "
                              "failed account/region run(s)")

        # Health that errored or couldn't be read isn't healthy, and firing alarms need a look.
        scores = {h.get('instance_id'): h.get('health_score') for h in ssm_health.get('results') or []}
        ssm_rec = ssm_health.get('recommendation')
        ssm_unread = bool(ssm_health.get('error')) or ssm_rec == 'UNKNOWN'
        if ssm_unread:
            unverified.append(f"Instance health couldn't be read ({ssm_health.get('error') or ssm_rec})")
        if alarm_health.get('unhealthy_instances', 0) > 0:
            unverified.append(f"CloudWatch alarms are firing on {alarm_health['unhealthy_instances']} instance(s)")
        elif alarm_health.get('recommendation') != 'HEALTHY':
            unverified.append("CloudWatch alarms couldn't be confirmed "
                              f"({alarm_health.get('error') or alarm_health.get('recommendation')})")

        for iid in instance_ids:
            state_check = patch_state_comparison.get(iid)
            state_match = state_check['reverted'] if state_check else None
            health_ok = scores.get(iid) == 100

            if state_match is False:
                unverified.append(f"{iid}: the patch state doesn't show the patches reverted")
            elif state_match is None and compare:
                unverified.append(f"{iid}: no patch state to compare before and after the rollback")
            if not health_ok and not ssm_unread:
                unverified.append(f"{iid}: the SSM agent isn't online" if iid in scores
                                  else f"{iid}: no health result")

            instance_results.append({
                'instance_id': iid,
                'patch_state': state_check if state_check else 'no_baseline',
                'patch_state_reverted': state_match,
                'health_ok': health_ok
            })

        if not unverified and not compare:
            # Without a snapshot nothing shows the patches came off, only that the run and the
            # instances are fine (disruption round, 2 Nov: this read as confirmed).
            overall_status = 'NOT_COMPARED'
            recommendation = 'HEALTHY_NOT_COMPARED'
            unverified.append("No patch state from before the rollback was given, so Patchy can't confirm the "
                              "patches were reverted; the run succeeded and the instances are healthy")
        elif not unverified:
            overall_status = 'VERIFIED'
            recommendation = 'ROLLBACK_CONFIRMED'
        else:
            overall_status = 'PARTIAL'
            recommendation = 'INVESTIGATE'

        logger.info(f"[TOOL:verify_rollback] RESULT: status={overall_status} recommendation={recommendation} "
                     f"run={run.get('status')} instances={len(instance_ids)}")

        return {
            'status': overall_status,
            'instances': instance_results,
            'run': _run_summary(run),
            'run_status': cmd_status,
            'ssm_health': ssm_health,
            'alarm_health': alarm_health,
            'recommendation': recommendation,
            **({'reasons': unverified[:10]} if unverified else {}),
            'message': f'Rollback {overall_status.lower()} on {len(instance_ids)} instances'
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"[TOOL:verify_rollback] ERROR: {error_info}")
        error_info["recommendation"] = "INVESTIGATE"
        return error_info
    except Exception as e:
        logger.error(f"Error verifying rollback: {e}")
        result = classify_error(e)
        result["recommendation"] = "INVESTIGATE"
        return result


@tool
def multi_account_dry_run(environment: str,
                          account_ids: List[str],
                          max_concurrency: Optional[str] = None,
                          max_errors: Optional[str] = None,
                          regions: Optional[List[str]] = None,
                          severity_filter: Optional[str] = None) -> dict:
    """Scan an environment across accounts for missing patches without installing.

    Use only when the operator asks to scan or preview a fleet scope. Don't call it before
    multi_account_execute; the patch plan is the preview.

    Args:
        environment: dev, staging or prod.
        account_ids: Accounts from resolve_execution_scope.
        max_concurrency: Accounts at once (default 50%).
        max_errors: Errors before stopping (default 25%).
        regions: Regions; defaults to the configured ones.
        severity_filter: CRITICAL, HIGH, MEDIUM or LOW.
    """
    try:
        env_value = _normalize_environment(environment)
        scope_error = _validate_account_scope(account_ids) or _validate_regions(regions)
        if scope_error:
            return scope_error
        env_tag, refused = _environment_target(env_value, account_ids)
        if refused:
            return refused
        target_regions = regions or SPOKE_REGIONS
        concurrency = max_concurrency or EXECUTION_DEFAULTS['account_max_concurrency']
        errors = max_errors or EXECUTION_DEFAULTS['account_max_errors']

        logger.info(f"[TOOL:multi_account_dry_run] env={env_value} accounts={len(account_ids)} "
                     f"regions={target_regions} severity={severity_filter}")

        if severity_filter and not _get_baseline_override_url(severity_filter):
            return {
                "status": "warning",
                "result_type": "error",
                "warning": f"Baseline override for '{severity_filter}' missing from S3.",
                "severity_filter_applied": False,
                "next_action": "The operation was NOT started. Present this to the operator.",
            }

        execution_id = _start_patch_automation(
            operation='Scan',
            environment=env_tag,
            account_ids=account_ids,
            regions=target_regions,
            max_concurrency=concurrency,
            max_errors=errors,
            severity_filter=severity_filter,
        )

        logger.info(f"[TOOL:multi_account_dry_run] INITIATED: execution_id={execution_id}")

        return {
            'automation_execution_id': execution_id,
            'environment': env_value,
            'total_accounts': len(account_ids),
            'severity_filter': severity_filter,
            'status': 'SCAN_INITIATED',
            'result_type': 'execution_started',
            'operation': 'scan',
            'estimated_duration': '2-3 minutes',
            'next_action': f"Started. Say what started and give execution ID {execution_id} exactly; it runs in the background and the operator can ask for status. Then stop.",
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"[TOOL:multi_account_dry_run] ERROR: {error_info}")
        return error_info
    except Exception as e:
        logger.error(f"[TOOL:multi_account_dry_run] ERROR: {e}")
        return classify_error(e)


def _multi_account_execute_impl(environment: str,
                          account_ids: List[str],
                          max_concurrency: Optional[str] = None,
                          max_errors: Optional[str] = None,
                          scan_execution_id: Optional[str] = None,
                          regions: Optional[List[str]] = None,
                          severity_filter: Optional[str] = None,
                          confirm_no_scan: bool = False,
                          confirm_execute: bool = False,
                          cve_id: Optional[str] = None,
                          severity: Optional[str] = None,
                          cvss_score: Optional[float] = None,
                          additional_cve_ids: Optional[List[str]] = None,
                          sla_hours: Optional[int] = None,
                          sla_source: Optional[str] = None,
                          first_observed_at: Optional[str] = None,
                          pre_patch_state: Optional[Dict[str, Any]] = None,
                          install_override_url: Optional[str] = None,
                          install_packages: Optional[List[str]] = None,
                          reboot_option: Optional[str] = None,
                          force_emergency: bool = False) -> dict:
    """Execute patching across accounts via SSM Automation. Returns one execution ID.

    Decision: Use when operator describes scope by environment, severity, or CVE without
    naming specific instance IDs. Always call resolve_execution_scope first.

    Code-enforced gates: requires max_concurrency, max_errors, and confirm_execute.
    scan_execution_id is optional -- if provided, verifies the scan completed successfully.
    If no scan_execution_id and confirm_no_scan is False, returns a warning asking operator to confirm.
    If confirm_execute is False, returns a confirmation request showing the execution plan.

    Pass cve_id, severity, cvss_score, sla_hours, sla_source, pre_patch_state when
    available -- they are forwarded to the compliance reconciliation pipeline so the
    final report (rendered by the UI when the execution completes) carries full context.

    Args:
        environment: Target environment
        account_ids: Account IDs from resolve_execution_scope
        max_concurrency: Accounts in parallel -- REQUIRED (e.g. "25%" or "10")
        max_errors: Stop threshold -- REQUIRED (e.g. "10%" or "3")
        scan_execution_id: Automation ID from multi_account_dry_run (optional -- skips verification if not provided)
        regions: Target regions (default: hub region)
        severity_filter: CRITICAL, HIGH, MEDIUM, or LOW
        cve_id: Primary CVE being remediated (forwarded to compliance report)
        severity: CVE severity (forwarded to compliance report)
        cvss_score: CVSS score (forwarded to compliance report)
        additional_cve_ids: Other CVEs fixed in same operation (forwarded to compliance report)
        sla_hours: SLA deadline (forwarded to compliance report)
        sla_source: SLA framework (forwarded to compliance report)
        pre_patch_state: From capture_patch_state (forwarded for before/after delta)

    Returns:
        dict: {automation_execution_id, environment, operator, total_accounts}
    """
    try:
        env_value = _normalize_environment(environment)
        target_regions = regions or SPOKE_REGIONS
        operator = get_operator()

        # Gate 0: accounts must be within the configured patch scope. The
        # tag-based path takes account_ids directly, so validate them here the
        # way the read paths already constrain to _get_configured_scope_accounts().
        account_scope_error = _validate_account_scope(account_ids) or _validate_regions(regions)
        if account_scope_error:
            return account_scope_error
        env_tag, refused = _environment_target(env_value, account_ids)
        if refused:
            return refused

        # Gate 1: operator must confirm concurrency/threshold
        if not max_concurrency or not max_errors:
            return {
                "error": "Operator must confirm max_concurrency and max_errors.",
                "error_code": "OperatorConfirmationRequired",
                "result_type": "error",
                "category": "ABORT",
                "retryable": False,
                "defaults": EXECUTION_DEFAULTS,
                "next_action": "The operation was NOT started. Present this to the operator.",
            }

        # A named CVE's severity comes from Inspector, not the model; if Inspector can't
        # be read, the model's value would set the SLA, so the plan stops instead.
        if cve_id:
            try:
                severity = _cve_severity([cve_id] + (additional_cve_ids or [])) or severity
            except _shared.InspectorUnreadable as e:
                return _inspector_unreadable(cve_id, e)

        # SLA from the strictest SLA-<severity> tag among the environment's
        # scoped instances, the same way instance-ID runs derive it.
        frameworks: List[str] = []
        env_instances = _environment_instance_ids(env_value, account_ids) if severity else []
        if severity and (sla_hours is None or sla_source is None):
            resolved = _resolve_sla_for_instances(env_instances, severity)
            sla_hours = sla_hours if sla_hours is not None else resolved.get('sla_hours')
            sla_source = sla_source or resolved.get('sla_source')
            frameworks = resolved.get('frameworks') or []

        # A maintenance window that patches the environment before the SLA
        # deadline wins, as for instance plans, unless the operator chose now.
        window_check, coverage = "", None
        if not sla_hours and severity_filter:
            # What the model reads, so it doesn't call this an emergency for want of a window.
            window_check = _SEVERITY_RUNS_NOW
        if not force_emergency and not confirm_execute and sla_hours and env_instances:
            scheduled, window_check, coverage = _scheduled_window(
                env_value, env_instances, sla_hours, sla_source, first_observed_at, cve_id, additional_cve_ids,
                reboot_option=reboot_option, install_packages=install_packages)
            if scheduled:
                return scheduled

        # Gate: operator must confirm execution intent (single confirmation)
        if not confirm_execute:
            why_now = _why_now(_run_now_reason(force_emergency, sla_hours, severity_filter, coverage))
            plan_warning = (
                f"About to patch environment '{env_value}' across accounts {account_ids} "
                f"in regions {target_regions}. Runs now, once approved: "
                f"{why_now}."
            )
            if not scan_execution_id:
                plan_warning += " No prior scan was performed -- patches will be applied based on the baseline."
            return {
                "status": "confirmation_required",
                "result_type": "gate_blocked",
                "warning": plan_warning,
                "execution_plan": {
                    "environment": env_value,
                    "accounts": account_ids,
                    "regions": target_regions,
                    "severity_filter": severity_filter,
                    "max_concurrency": max_concurrency,
                    "max_errors": max_errors,
                    "scan_execution_id": scan_execution_id,
                    "sla_hours": sla_hours,
                    "sla_source": sla_source,
                    "window_check": window_check,
                    "coverage": coverage,
                    "decision": "EMERGENCY",
                    "run_now_reason": _run_now_reason(force_emergency, sla_hours, severity_filter, coverage),
                    "why_now": why_now,
                },
                "question": "Confirm to proceed with patching, or cancel.",
                "error_code": "ExecutionConfirmation",
                "category": "ABORT",
                "retryable": True,
                "next_action": "Preview only: the plan tool stores this as a plan for the operator to approve; nothing runs until then.",
            }

        # Note: no-scan advisory removed. The confirmation plan (Gate 1b above)
        # already shows the full scope including scan_execution_id (or lack thereof).
        # One confirmation is sufficient -- don't second-guess the operator.

        # Gate 3: dry-run scan verification (if scan was provided), the same check the plan made
        if scan_execution_id:
            refused, _ = _scan_check(scan_execution_id, environment=env_value, account_ids=account_ids,
                                     regions=target_regions)
            if refused:
                return refused

        logger.info(f"[TOOL:multi_account_execute] env={env_value} "
                     f"accounts={len(account_ids)} concurrency={max_concurrency} "
                     f"errors={max_errors} severity={severity_filter} "
                     f"scan_id={scan_execution_id}")

        if severity_filter and not _get_baseline_override_url(severity_filter):
            return {
                "status": "warning",
                "result_type": "error",
                "warning": f"Baseline override for '{severity_filter}' missing from S3.",
                "next_action": "The operation was NOT started. Present this to the operator.",
            }

        # Every lookup happens before the start, so nothing that can fail sits
        # between starting the run and recording it. Without a CVE the discovery
        # time stays None and sla_met reports as "unknown".
        if first_observed_at is None:
            first_observed_at = _earliest_first_observed_at(
                [cve_id] + (additional_cve_ids or []),
                instance_ids=_environment_instance_ids(env_value, account_ids) or None)
        pending = {
            'operation_type': 'patch',
            'targeting': 'tag-based',
            'environment': env_value,
            'account_ids': account_ids,
            'regions': target_regions,
            'severity_filter': severity_filter,
            'install_packages': install_packages or [],
            'reboot_option': reboot_option or _reboot_option(env_value),
            'max_concurrency': max_concurrency,
            'max_errors': max_errors,
            'scan_execution_id': scan_execution_id,
            'decision': 'EMERGENCY',
            'decision_reason': _run_now_reason(force_emergency, sla_hours, severity_filter, None, checked=False),
            'cve_id': cve_id,
            'severity': severity,
            'cvss_score': cvss_score,
            'additional_cve_ids': additional_cve_ids,
            'sla_hours': sla_hours,
            'sla_source': sla_source,
            'first_observed_at': first_observed_at,
            'frameworks': frameworks,
            'pre_patch_state': _unwrap_patch_state(pre_patch_state),
        }

        execution_id = _start_patch_automation(
            operation='Install',
            environment=env_tag,
            account_ids=account_ids,
            regions=target_regions,
            max_concurrency=max_concurrency,
            max_errors=max_errors,
            severity_filter=severity_filter,
            install_override_url=install_override_url,
            reboot_option=reboot_option or _reboot_option(env_value),
        )
        _write_pending_compliance_context(execution_id, pending)

        logger.info(f"PATCH_EXECUTED: operator={operator} env={env_value} "
                     f"accounts={len(account_ids)} execution_id={execution_id}")

        return {
            'automation_execution_id': execution_id,
            'environment': env_value,
            'total_accounts': len(account_ids),
            'operator': operator,
            'severity_filter': severity_filter,
            'max_concurrency': max_concurrency,
            'max_errors': max_errors,
            'status': 'EXECUTING',
            'result_type': 'execution_started',
            'compliance_report_required': True,
            'operation': 'patch',
            'estimated_duration': '3-5 minutes',
            'next_action': f"Started. Say what started and give execution ID {execution_id} exactly; it runs in the background and the operator can ask for status. Then stop.",
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"[TOOL:multi_account_execute] ERROR: {error_info}")
        return error_info
    except Exception as e:
        logger.error(f"[TOOL:multi_account_execute] ERROR: {e}")
        return classify_error(e)


def _multi_account_rollback_impl(environment: str,
                           account_ids: List[str],
                           max_concurrency: Optional[str] = None,
                           max_errors: Optional[str] = None,
                           regions: Optional[List[str]] = None,
                           confirm_execute: bool = False,
                           since: Optional[int] = None,
                           until: Optional[int] = None) -> dict:
    """Rollback patches across accounts via SSM Automation. Returns one execution ID.

    Uses Patchy-RunRollback Automation doc (yum history undo -- Amazon Linux 2 only).
    Same TargetLocations pattern as multi_account_execute for consistent tracking.

    Code-enforced gates: requires max_concurrency, max_errors, and confirm_execute.
    First call returns a confirmation request showing the rollback plan; operator
    the runtime runs an approved plan with confirm_execute=True.

    Args:
        environment: Target environment
        account_ids: Account IDs to rollback
        max_concurrency: REQUIRED -- operator must confirm
        max_errors: REQUIRED -- operator must confirm
        regions: Target regions (default: hub region)
        confirm_execute: True to skip the confirmation gate. Default False
                         returns the plan instead of executing.

    Returns:
        dict: {automation_execution_id, environment, operator, total_accounts}
    """
    try:
        env_value = _normalize_environment(environment)
        target_regions = regions or SPOKE_REGIONS
        operator = get_operator()

        # Accounts must be within the configured patch scope (see
        # multi_account_execute — the tag-based path takes account_ids directly).
        account_scope_error = _validate_account_scope(account_ids) or _validate_regions(regions)
        if account_scope_error:
            return account_scope_error
        env_tag, refused = _environment_target(env_value, account_ids)
        if refused:
            return refused

        if not max_concurrency or not max_errors:
            return {
                "error": "Operator must confirm max_concurrency and max_errors for rollback.",
                "error_code": "OperatorConfirmationRequired",
                "result_type": "error",
                "category": "ABORT",
                "retryable": False,
                "defaults": EXECUTION_DEFAULTS,
                "next_action": "The operation was NOT started. Present this to the operator.",
            }

        # Gate: operator must confirm rollback intent. Rollback is destructive
        # and warrants the same explicit gate as install operations.
        if not confirm_execute:
            return {
                "status": "confirmation_required",
                "result_type": "gate_blocked",
                "warning": f"About to roll back patches in environment '{env_value}' across "
                           f"accounts {account_ids} in regions {target_regions}. "
                           f"This will undo the last yum transaction on each tagged instance.",
                "execution_plan": {
                    "environment": env_value,
                    "accounts": account_ids,
                    "regions": target_regions,
                    "max_concurrency": max_concurrency,
                    "max_errors": max_errors,
                    "operation": "rollback",
                },
                "question": "Confirm to proceed with rollback, or cancel.",
                "error_code": "ExecutionConfirmation",
                "category": "ABORT",
                "retryable": True,
                "next_action": "Preview only: the plan tool stores this as a plan for the operator to approve; nothing runs until then.",
            }

        logger.info(f"[TOOL:multi_account_rollback] env={env_value} accounts={len(account_ids)} "
                     f"operator={operator}")

        target_locations = [{
            'Accounts': account_ids,
            'Regions': target_regions,
            'ExecutionRoleName': SPOKE_EXECUTION_ROLE,
            'TargetLocationMaxConcurrency': max_concurrency,
            'TargetLocationMaxErrors': max_errors,
        }]

        resp = _ssm().start_automation_execution(
            DocumentName=ROLLBACK_DOC_NAME,
            Parameters={
                'Environment': [env_tag],
                'ScopeTagKey': [SCOPE_TAG_KEY],
                'ScopeTagValue': [SCOPE_TAG_VALUE],
                # Layer 3: inner SendCommand fan-out across tagged instances,
                # limited by the environment's rollout policy.
                'MaxConcurrency': [run_limits(env_value)['send_command_max_concurrency']],
                'MaxErrors':      [run_limits(env_value)['send_command_max_errors']],
                **_rollback_window_params(since, until),
            },
            TargetLocations=_with_location_alarms(target_locations, 'Rollback'),
            **_client_token(f"Rollback:tags:{','.join(account_ids)}:{','.join(target_regions)}"),
        )
        execution_id = resp['AutomationExecutionId']
        _started(execution_id)

        # Write pending compliance context for rollback reconciliation
        _write_pending_compliance_context(execution_id, {
            'operation_type': 'rollback',
            'targeting': 'tag-based',
            'environment': env_value,
            'account_ids': account_ids,
            'regions': target_regions,
            'max_concurrency': max_concurrency,
            'max_errors': max_errors,
        })

        logger.info(f"[TOOL:multi_account_rollback] INITIATED: execution_id={execution_id}")

        return {
            'automation_execution_id': execution_id,
            'environment': env_value,
            'operator': operator,
            'total_accounts': len(account_ids),
            'status': 'ROLLBACK_INITIATED',
            'result_type': 'execution_started',
            'operation': 'rollback',
            'estimated_duration': '3-5 minutes',
            'next_action': f"Started. Say what started and give execution ID {execution_id} exactly; it runs in the background and the operator can ask for status. Then stop.",
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"[TOOL:multi_account_rollback] ERROR: {error_info}")
        return error_info
    except Exception as e:
        logger.error(f"[TOOL:multi_account_rollback] ERROR: {e}")
        return classify_error(e)


def emergency_stop(automation_execution_id: str) -> dict:
    """Stop a Patchy run this conversation started. Called only by stop_run (Stop in the run view).

    Cancelling the run stops the instances not yet started, in every account and Region;
    an instance already being patched finishes. Not a model tool: the model can't stop runs.
    """
    try:
        if not automation_execution_id:
            return {"error": "Provide the execution ID of the run to stop.", "error_code": "InvalidParameter",
                    "category": "ABORT", "retryable": False}
        from helper import caller, plans
        if not caller.may_change():
            return {"error": "Only operators can stop runs.", "error_code": "NotOperator",
                    "category": "ABORT", "retryable": False}
        # Only the conversation that started a run can stop it.
        rec = plans.get_record(f"run-executions/{automation_execution_id}.json")
        if not rec or rec.get("session_id") != get_session_id():
            return {"error": f"{automation_execution_id} wasn't started from this conversation. Stop it from "
                             "the conversation that started it, or in the SSM console.",
                    "error_code": "RunNotFound", "category": "ABORT", "retryable": False}
        # Only Patchy's own work can be stopped from here.
        doc = _ssm().get_automation_execution(
            AutomationExecutionId=automation_execution_id)['AutomationExecution'].get('DocumentName', '')
        if not doc.startswith('Patchy-'):
            return {"error": f"{automation_execution_id} runs {doc or 'an unknown document'}, not a Patchy runbook.",
                    "error_code": "NotPatchyExecution", "category": "ABORT", "retryable": False}

        operator = get_operator()
        logger.info(f"[STOP] STOPPING automation={automation_execution_id} operator={operator}")
        _ssm().stop_automation_execution(AutomationExecutionId=automation_execution_id, Type='Cancel')
        return {
            'results': [{'stopped_id': automation_execution_id, 'type': 'automation',
                         'status': 'CANCEL_REQUESTED'}],
            'operator': operator,
            'message': 'Stop requested. Verify with get_automation_status.',
        }
    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"[STOP] ERROR: {error_info}")
        return error_info
    except Exception as e:
        logger.error(f"[STOP] ERROR: {e}")
        return classify_error(e)


@tool
def get_automation_status(automation_execution_id: str) -> dict:
    """Status of a patch, scan or rollback run, with per-account results.

    Use for "check status" on an execution ID.

    Args:
        automation_execution_id: Execution ID from a run card or scan.
    """
    try:
        logger.info(f"[TOOL:get_automation_status] execution_id={automation_execution_id}")

        # Parent execution
        parent = _ssm().get_automation_execution(
            AutomationExecutionId=automation_execution_id
        )['AutomationExecution']

        parent_status = parent['AutomationExecutionStatus']
        step_outputs = {}
        if parent.get('StepExecutions'):
            step = parent['StepExecutions'][0]
            step_outputs = {
                'step_name': step.get('StepName'),
                'step_status': step.get('StepStatus'),
                'command_id': step.get('Outputs', {}).get('CommandId', [None])[0],
            }

        # One child per (account, region). With TargetLocations the parent lists
        # one step per location ("<account>_<region>"); the hub's child listing
        # only shows the hub's own child, so the steps are the complete record.
        steps = list(parent.get('StepExecutions') or [])
        if parent.get('StepExecutionsTruncated'):
            steps = []
            for page in _ssm().get_paginator('describe_automation_step_executions').paginate(
                    AutomationExecutionId=automation_execution_id):
                steps.extend(page.get('StepExecutions') or [])
        children = []
        children_unreadable = None
        for st in steps:
            account, sep, region = str(st.get('StepName', '')).partition('_')
            if sep and account.isdigit() and len(account) == 12:
                status = st.get('StepStatus')
                if status in ('Pending', 'Waiting') and st.get('ExecutionStartTime'):
                    status = 'InProgress'  # started; SSM can leave a location step Pending while it patches
                children.append({'account': account, 'region': region, 'status': status})
        if not children:
            # Single-location runs have no per-location steps; list children instead.
            try:
                next_token = None
                while True:
                    kwargs = {'Filters': [{'Key': 'ParentExecutionId', 'Values': [automation_execution_id]}], 'MaxResults': 50}
                    if next_token:
                        kwargs['NextToken'] = next_token
                    child_resp = _ssm().describe_automation_executions(**kwargs)
                    for child in child_resp.get('AutomationExecutionMetadataList', []):
                        children.append({
                            'execution_id': child['AutomationExecutionId'],
                            'status': child['AutomationExecutionStatus'],
                            'account': child.get('ExecutedBy', ''),
                        })
                    next_token = child_resp.get('NextToken')
                    if not next_token:
                        break
            except ClientError as e:
                # Children may not exist yet if the parent is still starting; denied or throttled
                # means the locations' outcomes aren't known (2 Nov review, L8).
                code = e.response.get('Error', {}).get('Code', '')
                if 'NotFound' not in code:
                    children_unreadable = code or 'error'

        # Summarise
        total = len(children)
        succeeded = sum(1 for c in children if c['status'] == 'Success')
        failed = sum(1 for c in children if c['status'] in _RUN_FAILED_CHILD)
        cancelled = sum(1 for c in children if c['status'] in ('Cancelled', 'Cancelling'))
        in_progress = sum(1 for c in children if c['status'] in ('InProgress', 'Pending', 'Waiting'))

        logger.info(f"[TOOL:get_automation_status] parent={parent_status} "
                     f"children={total} success={succeeded} failed={failed} in_progress={in_progress}")

        return {
            'automation_execution_id': automation_execution_id,
            'status': parent_status,
            'step_outputs': step_outputs,
            'children_total': total,
            'children_succeeded': succeeded,
            'children_failed': failed,
            'children_cancelled': cancelled,
            'children_in_progress': in_progress,
            'children': children[:20],  # Cap to avoid token bloat
            **({'children_unreadable': children_unreadable} if children_unreadable else {}),
            'failure_message': parent.get('FailureMessage'),
            'ended_at': parent['ExecutionEndTime'].isoformat() if parent.get('ExecutionEndTime') else None,
            'started_at': parent['ExecutionStartTime'].isoformat() if parent.get('ExecutionStartTime') else None,
            **({'next_action': ("An operator stopped this run; it didn't fail. Say it was stopped, which "
                                "locations finished first, and that commands already sent may still finish. "
                                "Suggest Verify health.")}
               if parent_status in ('Cancelled', 'Cancelling') else {}),
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"[TOOL:get_automation_status] ERROR: {error_info}")
        return error_info
    except Exception as e:
        logger.error(f"[TOOL:get_automation_status] ERROR: {e}")
        return classify_error(e)
