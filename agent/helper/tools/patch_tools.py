"""Patch management tools: dry-run, execute, rollback, status, multi-account operations."""

from strands import tool
from typing import Optional, List, Dict, Any

from . import _shared
from ._shared import (
    logger, os, time, json,
    classify_error,
    ClientError,
    datetime, timedelta, timezone,
    get_client, get_hub_account_id,
    is_multi_account,
    resolve_scope,
    _ssm, _s3,
    _validate_instance_ids, _validate_instance_scope,
    _normalize_environment,
    _get_configured_scope_accounts,
    _get_query_targets,
    _get_fleet_summary,
    _group_instances_by_location,
    _resolve_sla_for_instances,
    _earliest_window_within_sla,
    _get_baseline_override_url,
    _get_compliance_bucket_name,
    _write_pending_compliance_context,
    _unwrap_patch_state,
    _start_patch_automation,
    _start_instance_patch_automation,
    _start_instance_rollback_automation,
    _format_utc_as_local,
    _calculate_sla_requirement,
    _current_request_scans,
    get_operator,
    AWS_REGION,
    SCOPE_TAG_KEY, SCOPE_TAG_VALUE,
    AUTOMATION_DOC_NAME, AUTOMATION_BY_ID_DOC_NAME,
    ROLLBACK_DOC_NAME, ROLLBACK_BY_ID_DOC_NAME,
    EXECUTION_DEFAULTS, SPOKE_EXECUTION_ROLE, SPOKE_REGIONS,
)


# ============================================================================
# TOOLS
# ============================================================================


@tool
def get_patch_compliance(environment: str,
                        instance_ids: Optional[List[str]] = None,
                        severity: Optional[str] = None) -> dict:
    """Get patch compliance status from AWS Systems Manager across all configured accounts and regions.

    Two modes:
    - environment-driven (instance_ids=None): discovers running instances tagged
      Environment=<env> AND PatchAutomation=enabled across every (account, region)
      in [hub] x SPOKE_REGIONS plus each spoke x SPOKE_REGIONS.
    - id-driven (instance_ids provided): looks each ID up in the fleet cache to find
      its (account, region), then queries SSM patch state in the right place.

    Args:
        environment: Filter by environment (dev, staging, prod) - REQUIRED for env-driven mode
        instance_ids: Specific instance IDs to check (skip env-driven discovery)
        severity: If provided, includes SLA calculation based on instance tags

    Returns:
        dict: {
            'instances': list (SSM patch data -- only populated after a scan has run),
            'total_count': int (instances WITH SSM patch data),
            'ec2_instance_ids': list (ALL running instances found via discovery),
            'ec2_instance_count': int,
            'unscanned_instance_ids': list (optional),
            'summary': dict,
            'sla_requirement': dict (optional),
            'queried_targets': list,
        }
    """
    try:
        instance_tags: Dict[str, Dict[str, str]] = {}
        # instance_locations[iid] = (account_id, region) -- populated during discovery
        instance_locations: Dict[str, tuple] = {}
        env_value = _normalize_environment(environment) if environment else None
        logger.info(f"[TOOL:get_patch_compliance] environment={environment} "
                    f"instance_ids={instance_ids[:5] if instance_ids else 'auto'} severity={severity}")

        # -- Mode 1: env-driven discovery across all (account, region) pairs --
        if not instance_ids:
            if not env_value:
                return {
                    "error": "environment is required when instance_ids is not provided",
                    "instances": [], "total_count": 0, "ec2_instance_ids": [],
                }
            instance_ids = []
            targets = _get_query_targets()

            def _discover_in_target(account_id: str, region: str) -> List[dict]:
                """Return [{'instance_id', 'tags', 'account_id', 'region'}, ...]"""
                try:
                    ec2 = get_client('ec2', account_id=account_id, region=region)
                except Exception as e:
                    logger.warning(f"[TOOL:get_patch_compliance] could not get EC2 client for {account_id}/{region}: {e}")
                    return []
                discovered: List[dict] = []
                try:
                    paginator = ec2.get_paginator('describe_instances')
                    for page in paginator.paginate(Filters=[
                        {'Name': 'tag:Environment', 'Values': [env_value]},
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
                return discovered

            from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
            with _TPE(max_workers=min(len(targets) or 1, 20)) as executor:
                futures = {executor.submit(_discover_in_target, a, r): (a, r) for (a, r) in targets}
                for fut in _ac(futures):
                    acct, rgn = futures[fut]
                    try:
                        discovered = fut.result(timeout=15)
                    except TimeoutError:
                        logger.warning(f"[TOOL:get_patch_compliance] Worker timed out for {acct}/{rgn}")
                        continue
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
            # Make sure the fleet cache is populated. _get_fleet_summary uses Explorer.
            try:
                _get_fleet_summary()
            except Exception:
                pass
            cache = _shared._fleet_instances_cache or {}
            for iid in instance_ids:
                cached = cache.get(iid)
                if cached:
                    instance_locations[iid] = (cached.get('account_id') or '', cached.get('region') or AWS_REGION)
            # IDs not in the cache get the hub fallback (preserves old behaviour)
            for iid in instance_ids:
                instance_locations.setdefault(iid, (None, AWS_REGION))

        if not instance_ids:
            return {"instances": [], "total_count": 0, "ec2_instance_ids": []}

        # -- Group by (account, region) and call describe_instance_patch_states per group --
        by_loc: Dict[tuple, List[str]] = {}
        for iid in instance_ids:
            by_loc.setdefault(instance_locations.get(iid, ('', AWS_REGION)), []).append(iid)

        instances: List[dict] = []
        ssm_found_ids: set = set()
        for (acct, rgn), ids in by_loc.items():
            try:
                ssm_client = get_client('ssm', account_id=acct or None, region=rgn)
            except Exception as e:
                logger.warning(f"[TOOL:get_patch_compliance] could not get SSM client for {acct}/{rgn}: {e}")
                continue
            for i in range(0, len(ids), 50):
                batch = ids[i:i+50]
                try:
                    response = ssm_client.describe_instance_patch_states(InstanceIds=batch)
                except Exception as e:
                    logger.warning(f"[TOOL:get_patch_compliance] patch-state lookup failed for {acct}/{rgn} batch: {e}")
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

        unscanned_ids = [iid for iid in instance_ids if iid not in ssm_found_ids]

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
    """Scan instances to preview missing patches via SSM Automation (MAMR).

    Decision: Use only when operator explicitly asks to scan/preview/dry-run named instances.
    Do NOT call as a pre-step to patching -- the install tool's confirmation IS the preview.

    Always uses Patchy-RunPatchBaselineById Automation document with TargetLocations
    for cross-account/cross-region execution. Polls for completion and returns
    per-instance patch details.

    Args:
        instance_ids: EC2 instance IDs to scan
        severity_filter: CRITICAL, HIGH, MEDIUM, or LOW -- scopes to that level and above

    Returns: {instances, total_missing, total_instances, automation_execution_id, severity_filter}
    """
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
                "next_action": "The operation was NOT started. Present this to the operator. Do NOT use get_response_template('operation_initiated').",
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
            'next_action': f"Use get_response_template('operation_initiated', execution_id='{execution_id}'). STOP after presenting.",
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


@tool
def execute_patch_operation(
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
    pre_patch_state: Optional[Dict[str, Any]] = None,
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
        scope_error = _validate_instance_scope(instance_ids)
        if scope_error:
            return scope_error

        logger.info(f"[TOOL:execute_patch_operation] mode={execution_mode} env={environment} "
                     f"instances={len(instance_ids)} severity_filter={severity_filter} "
                     f"scan_id={scan_execution_id} force_emergency={force_emergency} "
                     f"instance_ids={instance_ids[:5]}")

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
        if not force_emergency and not confirm_execute and sla_hours:
            window = _earliest_window_within_sla(environment, instance_ids, int(sla_hours))
            if window:
                return {
                    "status": "schedule_recommended",
                    "result_type": "gate_blocked",
                    "decision": "SCHEDULED",
                    "summary": (
                        f"A maintenance window opens in "
                        f"{window['hours_until_window']}h "
                        f"(within the {sla_hours}h SLA). The existing patch "
                        f"policy will install patches automatically -- no "
                        f"action needed unless you want to override."
                    ),
                    "sla_assessment": {
                        "sla_hours": sla_hours,
                        "sla_source": sla_source,
                        "next_window_name": window['name'],
                        "next_window_account": window['account_id'],
                        "next_window_region": window['region'],
                        "next_window_at_utc": window['next_execution'],
                        "hours_until_window": window['hours_until_window'],
                        "window_within_sla": True,
                    },
                    "options": [
                        "Wait for the window -- reply 'wait' or 'defer' (no further action)",
                        "Override and patch immediately -- reply 'patch now' or 'proceed' "
                        "(the LLM will retry with force_emergency=True)",
                    ],
                    "instance_ids": instance_ids,
                    "environment": environment,
                    "category": "ABORT",
                    "retryable": True,
                    "error_code": "ScheduleRecommended",
                    "next_action": "The operation was NOT started. Present this to the operator. Do NOT use get_response_template('operation_initiated').",
                }

        # Gate: operator must confirm execution intent
        if not confirm_execute:
            return {
                "status": "confirmation_required",
                "result_type": "gate_blocked",
                "warning": (
                    f"About to patch {len(instance_ids)} instance(s) in environment "
                    f"'{environment}'. SLA: {sla_hours}h ({sla_source or 'default'}). "
                    f"Decision: EMERGENCY. This will install patches immediately."
                ),
                "execution_plan": {
                    "instance_ids": instance_ids,
                    "environment": environment,
                    "execution_mode": execution_mode,
                    "severity_filter": severity_filter,
                    "scan_execution_id": scan_execution_id,
                    "sla_hours": sla_hours,
                    "sla_source": sla_source,
                    "decision": "EMERGENCY",
                },
                "question": "Confirm to proceed with patching, or cancel.",
                "to_proceed": "Call execute_patch_operation again with confirm_execute=True (and same parameters)",
                "error_code": "ExecutionConfirmation",
                "category": "ABORT",
                "retryable": True,
                "next_action": "The operation was NOT started. Present this plan verbatim to the operator. Do NOT use get_response_template('operation_initiated'). When they approve, call execute_patch_operation again with confirm_execute=True and the same parameters.",
            }

        # Note: no-scan advisory removed. The confirmation plan (Gate 1b above)
        # already shows the full scope. If no scan was done, the operator accepted
        # that when they confirmed. One confirmation is sufficient.

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
                "next_action": "The operation was NOT started. Present this to the operator. Do NOT use get_response_template('operation_initiated').",
            }

        # -- Dry-run gate: verify scan if provided (optional) --
        if scan_execution_id:
            try:
                scan_exec = _ssm().get_automation_execution(
                    AutomationExecutionId=scan_execution_id
                )['AutomationExecution']
                scan_status = scan_exec['AutomationExecutionStatus']
                if scan_status != 'Success':
                    return {
                        "error": f"Dry-run scan {scan_execution_id} status: {scan_status}. Must be Success.",
                        "error_code": "DryRunNotComplete",
                        "result_type": "error",
                        "category": "ABORT",
                        "retryable": False,
                        "next_action": "The operation was NOT started. Present this to the operator. Do NOT use get_response_template('operation_initiated').",
                    }
                # Check age (must be within 2 hours)
                exec_time = scan_exec.get('ExecutionStartTime')
                if exec_time:
                    if exec_time.tzinfo is None:
                        exec_time = exec_time.replace(tzinfo=timezone.utc)
                    hours_since = (datetime.now(timezone.utc) - exec_time).total_seconds() / 3600
                    if hours_since > 2:
                        return {
                            "error": f"Scan {scan_execution_id} is {hours_since:.1f}hr old.",
                            "error_code": "DryRunStale",
                            "result_type": "error",
                            "category": "ABORT",
                            "suggestion": "Run a fresh patch_dry_run.",
                            "retryable": False,
                            "next_action": "The operation was NOT started. Present this to the operator. Do NOT use get_response_template('operation_initiated').",
                        }
            except ClientError as e:
                return {
                    "error": f"Cannot verify dry-run scan: {e}",
                    "error_code": "DryRunVerificationFailed",
                    "result_type": "error",
                    "category": "ABORT",
                    "retryable": False,
                    "next_action": "The operation was NOT started. Present this to the operator. Do NOT use get_response_template('operation_initiated').",
                }

        # Severity override validation
        if severity_filter and not _get_baseline_override_url(severity_filter):
            return {
                "status": "warning",
                "result_type": "error",
                "warning": f"Cannot execute severity-scoped patching: baseline override "
                           f"file for '{severity_filter}' is missing from S3.",
                "suggestion": "Run setup_baseline_overrides.py, then retry.",
                "severity_filter_requested": severity_filter,
                "next_action": "The operation was NOT started. Present this to the operator. Do NOT use get_response_template('operation_initiated').",
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
                "next_action": "The operation was NOT started. Present this to the operator. Do NOT use get_response_template('operation_initiated').",
            }

        # -- Execute via MAMR (instance-ID based) --
        # Group instances by their owning (account, region) so each Automation
        # only targets the instances that actually live in that location.
        # Without this, a single Automation fans out to all (account, region)
        # children and each child receives every instance ID -- causing patches
        # against the wrong accounts to silently fail.
        groups = _group_instances_by_location(instance_ids)
        operator = get_operator()

        execution_ids: List[str] = []
        executed_account_ids: List[str] = []
        executed_regions: List[str] = []
        for (account, region), group_iids in groups.items():
            exec_id = _start_instance_patch_automation(
                operation='Install',
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
            logger.info(f"[TOOL:execute_patch_operation] Automation started: account={account} "
                        f"region={region} instances={len(group_iids)} exec_id={exec_id}")

        # Backwards-compat: surface first execution as `automation_execution_id`,
        # the full list as `automation_execution_ids`.
        execution_id = execution_ids[0] if execution_ids else ''
        account_ids = sorted(set(executed_account_ids))
        target_regions = sorted(set(executed_regions))

        # Write pending compliance context for later reconciliation.
        # One pending file per execution so the UI reconciler can correlate
        # status of each (account, region) child to its own report.
        for eid in execution_ids:
            _write_pending_compliance_context(eid, {
                'operation_type': 'patch',
                'targeting': 'instance-id',
                'environment': environment,
                'instance_ids': instance_ids,
                'account_ids': account_ids,
                'regions': target_regions,
                'severity_filter': severity_filter,
                'execution_mode': execution_mode,
                'scan_execution_id': scan_execution_id,
                'decision': 'EMERGENCY' if execution_mode == 'immediate' else 'SCHEDULED',
                'cve_id': cve_id,
                'severity': severity,
                'cvss_score': cvss_score,
                'additional_cve_ids': additional_cve_ids,
                'sla_hours': sla_hours,
                'sla_source': sla_source,
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
            'next_action': f"Use get_response_template('operation_initiated', execution_id='{execution_id}'). STOP after presenting.",
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
    """Get status of SSM command execution.

    Args:
        command_id: SSM command ID to check
        instance_ids: List of instance IDs the command was sent to

    Returns:
        dict: {'results': list, 'summary': dict, 'overall_status': str}
    """
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
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
        with ThreadPoolExecutor(max_workers=min(10, len(instance_ids))) as executor:
            futures = {executor.submit(get_instance_status, iid): iid for iid in instance_ids}
            for future in as_completed(futures):
                iid = futures[future]
                try:
                    results.append(future.result(timeout=15))
                except TimeoutError:
                    logger.warning(f"[TOOL:get_command_status] Worker timed out for {iid}")
                    results.append({'instance_id': iid, 'status': 'Unknown', 'output': 'timeout'})
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


@tool
def rollback_patches(instance_ids: List[str], confirm_execute: bool = False) -> dict:
    """Rollback patches on EC2 instances via SSM Automation (MAMR).

    Decision: Use when operator names specific instance IDs for rollback.
    For fleet-scope rollback, use multi_account_rollback instead.

    Uses Patchy-RunRollbackById Automation document with instance-ID targeting.
    This only undoes the LAST yum transaction on each instance.

    Code-enforced gate: requires confirm_execute=True. First call returns a
    confirmation request showing the rollback plan. Operator approval triggers
    a second call with confirm_execute=True to actually execute.

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
        scope_error = _validate_instance_scope(instance_ids)
        if scope_error:
            return scope_error

        # Gate: operator must confirm rollback intent. Rollback is destructive
        # (yum history undo on each instance) and warrants the same explicit
        # gate as install operations.
        if not confirm_execute:
            return {
                "status": "confirmation_required",
                "result_type": "gate_blocked",
                "warning": f"About to roll back patches on {len(instance_ids)} instance(s). "
                           f"This will undo the last yum transaction on each instance.",
                "execution_plan": {
                    "instance_ids": instance_ids,
                    "operation": "rollback",
                },
                "question": "Confirm to proceed with rollback, or cancel.",
                "to_proceed": "Call rollback_patches again with confirm_execute=True (and same instance_ids)",
                "error_code": "ExecutionConfirmation",
                "category": "ABORT",
                "retryable": True,
                "next_action": "The operation was NOT started. Present this rollback plan verbatim to the operator. Do NOT use get_response_template('operation_initiated'). When they approve, call rollback_patches again with confirm_execute=True and the same instance_ids.",
            }

        logger.info(f"[TOOL:rollback_patches] instances={len(instance_ids)} instance_ids={instance_ids[:5]}")

        # Group instances by their owning (account, region) so each Automation
        # only targets the instances that actually live in that location.
        groups = _group_instances_by_location(instance_ids)

        execution_ids: List[str] = []
        executed_account_ids: List[str] = []
        executed_regions: List[str] = []
        for (account, region), group_iids in groups.items():
            exec_id = _start_instance_rollback_automation(
                instance_ids=group_iids,
                account_ids=[account],
                regions=[region],
                max_concurrency=EXECUTION_DEFAULTS['account_max_concurrency'],
                max_errors=EXECUTION_DEFAULTS['account_max_errors'],
            )
            execution_ids.append(exec_id)
            executed_account_ids.append(account)
            executed_regions.append(region)
            logger.info(f"[TOOL:rollback_patches] Automation started: account={account} "
                        f"region={region} instances={len(group_iids)} exec_id={exec_id}")

        execution_id = execution_ids[0] if execution_ids else ''
        account_ids = sorted(set(executed_account_ids))
        target_regions = sorted(set(executed_regions))

        # Write pending compliance context for rollback reconciliation
        for eid in execution_ids:
            _write_pending_compliance_context(eid, {
                'operation_type': 'rollback',
                'targeting': 'instance-id',
                'environment': 'unknown',
                'instance_ids': instance_ids,
                'account_ids': account_ids,
                'regions': target_regions,
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
            'next_action': f"Use get_response_template('operation_initiated', execution_id='{execution_id}'). STOP after presenting.",
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


@tool
def verify_rollback(command_id: str, instance_ids: List[str],
                    pre_patch_state: Optional[Dict[str, Any]] = None,
                    post_patch_state: Optional[Dict[str, Any]] = None) -> dict:
    """Verify rollback succeeded. Call after rollback_patches completes.

    Re-scans instances, compares patch state against snapshots, checks health.
    Pass post_patch_state for most reliable comparison.

    Args:
        command_id: SSM command ID from rollback_patches
        instance_ids: Instance IDs that were rolled back
        pre_patch_state: From capture_patch_state BEFORE patching (optional)
        post_patch_state: From capture_patch_state AFTER patching (preferred)

    Returns: {status: VERIFIED|PARTIAL|FAILED, instances, recommendation}
    """
    try:
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error
        scope_error = _validate_instance_scope(instance_ids)
        if scope_error:
            return scope_error

        logger.info(f"[TOOL:verify_rollback] command_id={command_id} instances={len(instance_ids)} "
                     f"has_pre_patch={pre_patch_state is not None} has_post_patch={post_patch_state is not None}")

        # Step 1: Wait for rollback command to complete (max 5 min)
        status_result = None
        for _ in range(30):
            time.sleep(10)  # nosemgrep: arbitrary-sleep
            status_result = get_command_status(command_id, instance_ids)
            if status_result.get('status') in ('Success', 'Failed'):
                break

        if not status_result or status_result.get('status') not in ('Success', 'Failed'):
            return {
                'status': 'TIMEOUT',
                'recommendation': 'INVESTIGATE',
                'reason': 'Rollback command did not complete within 5 minutes',
                'command_status': status_result
            }

        # Step 2: Re-scan with RunPatchBaseline to get current state
        patch_state_comparison = {}
        pre_patch_state = _unwrap_patch_state(pre_patch_state)
        post_patch_state = _unwrap_patch_state(post_patch_state)
        has_comparison_data = pre_patch_state or post_patch_state
        if status_result.get('status') == 'Success' and has_comparison_data:
            try:
                # Run the same scan as patch_dry_run
                scan_response = _ssm().send_command(
                    DocumentName='AWS-RunPatchBaseline',
                    InstanceIds=instance_ids,
                    Parameters={'Operation': ['Scan']},
                    Comment='Post-rollback verification scan'
                )
                scan_cmd_id = scan_response['Command']['CommandId']

                # Wait for scan (max 3 min)
                for _ in range(18):
                    time.sleep(10)  # nosemgrep: arbitrary-sleep
                    try:
                        cmd_resp = _ssm().list_command_invocations(
                            CommandId=scan_cmd_id, Details=False
                        )
                        invs = cmd_resp.get('CommandInvocations', [])
                        if invs and all(i['Status'] in ('Success', 'Failed') for i in invs):
                            break
                    except Exception:
                        continue

                # Compare current patch state against snapshots
                current_state = _ssm().describe_instance_patch_states(
                    InstanceIds=instance_ids
                )
                current_map = {
                    s['InstanceId']: s
                    for s in current_state.get('InstancePatchStates', [])
                }

                for iid in instance_ids:
                    after_raw = current_map.get(iid, {})
                    after = {
                        'missing_count': after_raw.get('MissingCount', 0),
                        'installed_count': after_raw.get('InstalledCount', 0),
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

        # Log comparison results
        for iid, comp in patch_state_comparison.items():
            logger.info(f"[TOOL:verify_rollback] COMPARISON {iid}: basis={comp.get('comparison_basis','none')} reverted={comp.get('reverted','N/A')}")

        # Step 3: Health checks
        from .maintenance_tools import check_instance_health, check_cloudwatch_alarms
        ssm_health = check_instance_health(instance_ids)
        alarm_health = check_cloudwatch_alarms(instance_ids)

        # Build per-instance results
        instance_results = []
        all_verified = True
        any_failed = False

        for iid in instance_ids:
            cmd_status = 'Unknown'
            for r in status_result.get('results', []):
                if r.get('instance_id') == iid:
                    cmd_status = r.get('status', 'Unknown')
                    break

            state_check = patch_state_comparison.get(iid)
            state_match = state_check['reverted'] if state_check else None

            health_ok = True
            for h in ssm_health.get('results', []):
                if h.get('instance_id') == iid and h.get('health_score', 0) < 100:
                    health_ok = False

            if cmd_status != 'Success' or state_match is False or not health_ok:
                all_verified = False
            if cmd_status == 'Failed':
                any_failed = True

            instance_results.append({
                'instance_id': iid,
                'command_status': cmd_status,
                'patch_state': state_check if state_check else 'no_baseline',
                'patch_state_reverted': state_match,
                'health_ok': health_ok
            })

        if any_failed:
            overall_status = 'FAILED'
            recommendation = 'MANUAL_INTERVENTION'
        elif all_verified:
            overall_status = 'VERIFIED'
            recommendation = 'ROLLBACK_CONFIRMED'
        else:
            overall_status = 'PARTIAL'
            recommendation = 'INVESTIGATE'

        logger.info(f"[TOOL:verify_rollback] RESULT: status={overall_status} recommendation={recommendation} "
                     f"cmd_status={status_result.get('status')} instances={len(instance_ids)}")

        return {
            'status': overall_status,
            'instances': instance_results,
            'command_status': status_result.get('status'),
            'ssm_health': ssm_health,
            'alarm_health': alarm_health,
            'recommendation': recommendation,
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
    """Initiate dry-run scan across accounts via SSM Automation. Returns one execution ID.

    Decision: Use only when operator explicitly asks to preview/scan a fleet scope.
    Do NOT call before multi_account_execute -- the install tool handles confirmation.

    Args:
        environment: Target environment
        account_ids: Account IDs from resolve_execution_scope
        max_concurrency: Account concurrency (default: 50%)
        max_errors: Error threshold (default: 25%)
        regions: Target regions (default: hub region)
        severity_filter: CRITICAL, HIGH, MEDIUM, or LOW

    Returns:
        dict: {automation_execution_id, environment, total_accounts}
    """
    try:
        env_value = _normalize_environment(environment)
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
                "next_action": "The operation was NOT started. Present this to the operator. Do NOT use get_response_template('operation_initiated').",
            }

        execution_id = _start_patch_automation(
            operation='Scan',
            environment=env_value,
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
            'next_action': f"Use get_response_template('operation_initiated', execution_id='{execution_id}'). STOP after presenting.",
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"[TOOL:multi_account_dry_run] ERROR: {error_info}")
        return error_info
    except Exception as e:
        logger.error(f"[TOOL:multi_account_dry_run] ERROR: {e}")
        return classify_error(e)


@tool
def multi_account_execute(environment: str,
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
                          pre_patch_state: Optional[Dict[str, Any]] = None) -> dict:
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

        # Gate 1: operator must confirm concurrency/threshold
        if not max_concurrency or not max_errors:
            return {
                "error": "Operator must confirm max_concurrency and max_errors.",
                "error_code": "OperatorConfirmationRequired",
                "result_type": "error",
                "category": "ABORT",
                "retryable": False,
                "defaults": EXECUTION_DEFAULTS,
                "next_action": "The operation was NOT started. Present this to the operator. Do NOT use get_response_template('operation_initiated').",
            }

        # Gate: operator must confirm execution intent (single confirmation)
        if not confirm_execute:
            plan_warning = (
                f"About to patch environment '{env_value}' across accounts {account_ids} "
                f"in regions {target_regions}. This will install patches."
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
                },
                "question": "Confirm to proceed with patching, or cancel.",
                "to_proceed": "Call multi_account_execute again with confirm_execute=True (and same parameters)",
                "error_code": "ExecutionConfirmation",
                "category": "ABORT",
                "retryable": True,
                "next_action": "The operation was NOT started. Present this plan verbatim to the operator. Do NOT use get_response_template('operation_initiated'). When they approve, call multi_account_execute again with confirm_execute=True and the same parameters.",
            }

        # Note: no-scan advisory removed. The confirmation plan (Gate 1b above)
        # already shows the full scope including scan_execution_id (or lack thereof).
        # One confirmation is sufficient -- don't second-guess the operator.

        # Gate 3: dry-run scan verification (if scan was provided)
        if scan_execution_id:
            try:
                scan_status = _ssm().get_automation_execution(
                    AutomationExecutionId=scan_execution_id
                )['AutomationExecution']['AutomationExecutionStatus']
                if scan_status != 'Success':
                    return {
                        "error": f"Dry-run scan {scan_execution_id} status: {scan_status}. Must be Success.",
                        "error_code": "DryRunNotComplete",
                        "result_type": "error",
                        "category": "ABORT",
                        "retryable": False,
                        "next_action": "The operation was NOT started. Present this to the operator. Do NOT use get_response_template('operation_initiated').",
                    }
            except ClientError as e:
                return {
                    "error": f"Cannot verify dry-run scan: {e}",
                    "error_code": "DryRunVerificationFailed",
                    "result_type": "error",
                    "category": "ABORT",
                    "retryable": False,
                    "next_action": "The operation was NOT started. Present this to the operator. Do NOT use get_response_template('operation_initiated').",
                }

        logger.info(f"[TOOL:multi_account_execute] env={env_value} "
                     f"accounts={len(account_ids)} concurrency={max_concurrency} "
                     f"errors={max_errors} severity={severity_filter} "
                     f"scan_id={scan_execution_id}")

        if severity_filter and not _get_baseline_override_url(severity_filter):
            return {
                "status": "warning",
                "result_type": "error",
                "warning": f"Baseline override for '{severity_filter}' missing from S3.",
                "next_action": "The operation was NOT started. Present this to the operator. Do NOT use get_response_template('operation_initiated').",
            }

        execution_id = _start_patch_automation(
            operation='Install',
            environment=env_value,
            account_ids=account_ids,
            regions=target_regions,
            max_concurrency=max_concurrency,
            max_errors=max_errors,
            severity_filter=severity_filter,
        )

        # Write pending compliance context for later reconciliation
        _write_pending_compliance_context(execution_id, {
            'operation_type': 'patch',
            'targeting': 'tag-based',
            'environment': env_value,
            'account_ids': account_ids,
            'regions': target_regions,
            'severity_filter': severity_filter,
            'max_concurrency': max_concurrency,
            'max_errors': max_errors,
            'scan_execution_id': scan_execution_id,
            'decision': 'EMERGENCY',
            'cve_id': cve_id,
            'severity': severity,
            'cvss_score': cvss_score,
            'additional_cve_ids': additional_cve_ids,
            'sla_hours': sla_hours,
            'sla_source': sla_source,
            'pre_patch_state': _unwrap_patch_state(pre_patch_state),
        })

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
            'next_action': f"Use get_response_template('operation_initiated', execution_id='{execution_id}'). STOP after presenting.",
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"[TOOL:multi_account_execute] ERROR: {error_info}")
        return error_info
    except Exception as e:
        logger.error(f"[TOOL:multi_account_execute] ERROR: {e}")
        return classify_error(e)


@tool
def multi_account_rollback(environment: str,
                           account_ids: List[str],
                           max_concurrency: Optional[str] = None,
                           max_errors: Optional[str] = None,
                           regions: Optional[List[str]] = None,
                           confirm_execute: bool = False) -> dict:
    """Rollback patches across accounts via SSM Automation. Returns one execution ID.

    Uses Patchy-RunRollback Automation doc (yum history undo -- Amazon Linux 2 only).
    Same TargetLocations pattern as multi_account_execute for consistent tracking.

    Code-enforced gates: requires max_concurrency, max_errors, and confirm_execute.
    First call returns a confirmation request showing the rollback plan; operator
    approval triggers a second call with confirm_execute=True.

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

        if not max_concurrency or not max_errors:
            return {
                "error": "Operator must confirm max_concurrency and max_errors for rollback.",
                "error_code": "OperatorConfirmationRequired",
                "result_type": "error",
                "category": "ABORT",
                "retryable": False,
                "defaults": EXECUTION_DEFAULTS,
                "next_action": "The operation was NOT started. Present this to the operator. Do NOT use get_response_template('operation_initiated').",
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
                "to_proceed": "Call multi_account_rollback again with confirm_execute=True (and same parameters)",
                "error_code": "ExecutionConfirmation",
                "category": "ABORT",
                "retryable": True,
                "next_action": "The operation was NOT started. Present this rollback plan verbatim to the operator. Do NOT use get_response_template('operation_initiated'). When they approve, call multi_account_rollback again with confirm_execute=True and the same parameters.",
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
                'Environment': [env_value],
                'ScopeTagKey': [SCOPE_TAG_KEY],
                'ScopeTagValue': [SCOPE_TAG_VALUE],
                # Layer 3: inner SendCommand fan-out across tagged instances.
                'MaxConcurrency': [EXECUTION_DEFAULTS['send_command_max_concurrency']],
                'MaxErrors':      [EXECUTION_DEFAULTS['send_command_max_errors']],
            },
            TargetLocations=target_locations,
        )
        execution_id = resp['AutomationExecutionId']

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
            'next_action': f"Use get_response_template('operation_initiated', execution_id='{execution_id}'). STOP after presenting.",
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"[TOOL:multi_account_rollback] ERROR: {error_info}")
        return error_info
    except Exception as e:
        logger.error(f"[TOOL:multi_account_rollback] ERROR: {e}")
        return classify_error(e)


@tool
def emergency_stop(automation_execution_id: Optional[str] = None,
                   command_id: Optional[str] = None) -> dict:
    """Stop a running patch operation (Automation execution or SSM command).

    Args:
        automation_execution_id: Automation execution ID to stop
        command_id: SSM command ID to cancel

    Returns:
        dict: {results: [{stopped_id, type, status}], operator}
    """
    try:
        if not automation_execution_id and not command_id:
            return {
                "error": "Provide automation_execution_id or command_id to stop.",
                "error_code": "InvalidParameter",
                "category": "ABORT",
                "retryable": False,
            }

        operator = get_operator()
        results = []

        if automation_execution_id:
            logger.info(f"[TOOL:emergency_stop] STOPPING automation={automation_execution_id} "
                         f"operator={operator}")
            _ssm().stop_automation_execution(
                AutomationExecutionId=automation_execution_id,
                Type='Cancel',
            )
            results.append({
                'stopped_id': automation_execution_id,
                'type': 'automation',
                'status': 'CANCEL_REQUESTED',
            })

        if command_id:
            logger.info(f"[TOOL:emergency_stop] CANCELLING command={command_id} "
                         f"operator={operator}")
            _ssm().cancel_command(CommandId=command_id)
            results.append({
                'stopped_id': command_id,
                'type': 'command',
                'status': 'CANCEL_REQUESTED',
            })

        logger.info(f"EMERGENCY_STOP: operator={operator} stopped={len(results)} operations")

        return {
            'results': results,
            'operator': operator,
            'message': f'Stopped {len(results)} operation(s). Verify with get_automation_status or get_command_status.',
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"[TOOL:emergency_stop] ERROR: {error_info}")
        return error_info
    except Exception as e:
        logger.error(f"[TOOL:emergency_stop] ERROR: {e}")
        return classify_error(e)


@tool
def get_automation_status(automation_execution_id: str) -> dict:
    """Get status of a cross-account Automation execution and its per-account children.

    Args:
        automation_execution_id: Parent execution ID from multi_account_dry_run, multi_account_execute, or multi_account_rollback

    Returns:
        dict: {status, children: [{account, region, status}], summary}
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

        # Child executions (one per account/region pair) -- paginated
        children = []
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
        except ClientError:
            pass  # Children may not exist yet if parent is still starting

        # Summarise
        total = len(children)
        succeeded = sum(1 for c in children if c['status'] == 'Success')
        failed = sum(1 for c in children if c['status'] == 'Failed')
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
            'children_in_progress': in_progress,
            'children': children[:20],  # Cap to avoid token bloat
            'failure_message': parent.get('FailureMessage'),
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"[TOOL:get_automation_status] ERROR: {error_info}")
        return error_info
    except Exception as e:
        logger.error(f"[TOOL:get_automation_status] ERROR: {e}")
        return classify_error(e)
