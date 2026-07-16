"""Compliance and reporting tools: patch state capture, CVE verification, report queries."""

from strands import tool
from typing import Optional, List, Dict, Any

from . import _shared
from ._shared import (
    logger, os, json,
    classify_error,
    ClientError,
    datetime, timedelta, timezone,
    get_client,
    _ssm, _s3, _inspector,
    _validate_instance_ids,
    _normalize_environment,
    _get_fleet_summary,
    _get_compliance_bucket_name,
    _get_date_prefix,
    AWS_REGION,
)


# ============================================================================
# LOCAL HELPERS (only used by tools in this module)
# ============================================================================


def _fetch_reports_from_s3(s3_client, bucket_name: str, start_date: datetime,
                           end_date: datetime, severity: Optional[str],
                           environment: Optional[str], sla_breaches_only: bool) -> list:
    """Fetch and filter reports from S3 with optimized month-prefix queries."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Collect unique year/month prefixes covering the date range (few API calls)
    months: set[str] = set()
    d = start_date
    while d <= end_date:
        months.add(f"{d.year}/{d.month:02d}/")
        d += timedelta(days=28)
    months.add(f"{end_date.year}/{end_date.month:02d}/")

    objects_to_check = []
    for prefix in sorted(months):
        try:
            paginator = s3_client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
                for obj in page.get("Contents", []):
                    # Filter by actual date cutoff
                    obj_date = obj["LastModified"]
                    if obj_date.tzinfo is None:
                        obj_date = obj_date.replace(tzinfo=timezone.utc)
                    if start_date <= obj_date <= end_date:
                        objects_to_check.append(obj)
        except Exception as e:
            logger.debug(f"No reports for {prefix}: {e}")
            continue

    if not objects_to_check:
        return []

    # Parallel head_object calls (max 10 concurrent)
    def fetch_metadata(obj):
        return _parse_report_metadata(s3_client, bucket_name, obj)

    reports = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_metadata, obj): obj for obj in objects_to_check}
        for future in as_completed(futures):
            obj = futures[future]
            try:
                report = future.result(timeout=15)
            except TimeoutError:
                logger.warning(f"[TOOL:_fetch_reports_from_s3] Worker timed out for {obj.get('Key', 'unknown')}")
                continue
            except Exception as e:
                logger.warning(f"[TOOL:_fetch_reports_from_s3] Worker failed for {obj.get('Key', 'unknown')}: {e}")
                continue
            if report and _matches_filters(report, severity, environment, sla_breaches_only):
                reports.append(report)

    return reports


def _parse_report_metadata(s3_client, bucket_name: str, obj: dict) -> Optional[dict]:
    """Parse S3 object metadata into report dict (KISS helper)."""
    try:
        metadata_response = s3_client.head_object(Bucket=bucket_name, Key=obj['Key'])
        metadata = metadata_response.get('Metadata', {})

        # Parse sla-met: 'True' -> True, 'False' -> False, 'UNKNOWN'/missing -> None
        sla_met_raw = metadata.get('sla-met', 'UNKNOWN')
        if sla_met_raw == 'True':
            sla_met = True
        elif sla_met_raw == 'False':
            sla_met = False
        else:
            sla_met = None  # Unknown -- SLA was not calculated

        # Frameworks are stored as a comma-separated string in S3 metadata.
        frameworks_raw = metadata.get('frameworks', '') or ''
        frameworks = [fw.strip() for fw in frameworks_raw.split(',') if fw.strip()]

        return {
            'report_id': obj['Key'].split('/')[-1].replace('.json', ''),
            'cve_id': metadata.get('cve-id', 'unknown'),
            'severity': metadata.get('severity', 'unknown'),
            'environment': metadata.get('environment', 'unknown'),
            'decision': metadata.get('decision-type', 'unknown'),
            'sla_met': sla_met,
            'frameworks': frameworks,
            'team': metadata.get('team', 'unknown'),
            'product': metadata.get('product', 'unknown'),
            's3_key': obj['Key'],
            'generated_at': obj['LastModified'].isoformat()
        }
    except Exception as e:
        logger.warning(f"Could not parse metadata for {obj['Key']}: {e}")
        return None


def _matches_filters(report: dict, severity: Optional[str], environment: Optional[str],
                     sla_breaches_only: bool) -> bool:
    """Check if report matches filter criteria (KISS helper)."""
    if severity and report['severity'].upper() != severity.upper():
        return False
    if environment and report['environment'] != environment:
        return False
    if sla_breaches_only:
        # Only include confirmed breaches (sla_met=False), not unknown (sla_met=None)
        if report['sla_met'] is None or report['sla_met'] is True:
            return False
    return True


def _calculate_report_statistics(reports: list) -> dict:
    """Calculate summary statistics from reports (KISS helper)."""
    total = len(reports)
    if total == 0:
        return {
            'total_reports': 0,
            'sla_breaches': 0,
            'sla_breach_rate': 0,
            'sla_unknown': 0,
            'by_severity': {},
            'by_team': {},
            'by_decision': {}
        }

    sla_breaches = sum(1 for r in reports if r['sla_met'] is False)
    sla_unknown = sum(1 for r in reports if r['sla_met'] is None)
    # Breach rate calculated only against reports with known SLA
    reports_with_sla = total - sla_unknown
    breach_rate = (sla_breaches / reports_with_sla * 100) if reports_with_sla > 0 else 0

    # Breakdown by severity
    severity_breakdown = {}
    for r in reports:
        sev = r['severity']
        severity_breakdown[sev] = severity_breakdown.get(sev, 0) + 1

    # Breakdown by team
    team_breakdown = {}
    for r in reports:
        team = r['team']
        team_breakdown[team] = team_breakdown.get(team, 0) + 1

    # Breakdown by decision type
    decision_breakdown = {}
    for r in reports:
        dec = r['decision']
        decision_breakdown[dec] = decision_breakdown.get(dec, 0) + 1

    return {
        'total_reports': total,
        'sla_breaches': sla_breaches,
        'sla_breach_rate': round(breach_rate, 1),
        'sla_unknown': sla_unknown,
        'by_severity': severity_breakdown,
        'by_team': team_breakdown,
        'by_decision': decision_breakdown
    }


# ============================================================================
# TOOLS
# ============================================================================


@tool
def capture_patch_state(instance_ids: List[str]) -> dict:
    """Capture current patch compliance state for instances (snapshot).

    IMPORTANT: This reads DescribeInstancePatchStates which is only populated
    AFTER a scan (patch_dry_run) has been run. Always call patch_dry_run FIRST,
    then capture_patch_state. Without a prior scan, patch state data may be
    empty or stale.

    Call this BEFORE patching to capture pre-patch state, and AFTER patching
    to capture post-patch state. Both snapshots are forwarded to the UI's
    compliance reconciliation pipeline for before/after delta in the final
    compliance report (rendered in the Compliance tab).

    Args:
        instance_ids: List of EC2 instance IDs to snapshot

    Returns:
        dict: {
            'snapshot': {instance_id: {missing_count, installed_count, security_non_compliant}},
            'timestamp': str,
            'total_missing': int
        }
    """
    try:
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error

        logger.info(f"[TOOL:capture_patch_state] instances={len(instance_ids)} instance_ids={instance_ids[:5]}")

        # Group by (account, region) for cross-region support
        _get_fleet_summary()  # ensure cache is warm
        cache = _shared._fleet_instances_cache or {}
        by_location: Dict[tuple, list] = {}
        for iid in instance_ids:
            cached = cache.get(iid)
            if cached:
                loc = (cached.get('account_id'), cached.get('region') or AWS_REGION)
            else:
                loc = (None, AWS_REGION)
            by_location.setdefault(loc, []).append(iid)

        snapshot = {}
        total_missing = 0
        total_installed = 0

        for (account_id, region), iids in by_location.items():
            try:
                ssm_client = get_client('ssm', account_id=account_id, region=region)
                for i in range(0, len(iids), 50):
                    batch = iids[i:i + 50]
                    response = ssm_client.describe_instance_patch_states(InstanceIds=batch)
                    for state in response.get('InstancePatchStates', []):
                        iid = state['InstanceId']
                        missing = state.get('MissingCount', 0)
                        installed = state.get('InstalledCount', 0)
                        total_missing += missing
                        total_installed += installed
                        snapshot[iid] = {
                            'missing_count': missing,
                            'installed_count': installed,
                            'security_non_compliant': state.get('SecurityNonCompliantCount', 0),
                            'failed_count': state.get('FailedCount', 0)
                        }
            except Exception as e:
                logger.warning(f"[TOOL:capture_patch_state] Failed for {account_id}/{region}: {e}")

        # Include instances with no patch state data
        for iid in instance_ids:
            if iid not in snapshot:
                snapshot[iid] = {
                    'missing_count': 0,
                    'installed_count': 0,
                    'security_non_compliant': 0,
                    'failed_count': 0
                }

        logger.info(f"[TOOL:capture_patch_state] RESULT: instances={len(snapshot)} total_missing={total_missing} "
                     f"total_installed={total_installed} no_data={len([iid for iid in instance_ids if iid not in snapshot])}")

        return {
            'snapshot': snapshot,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'total_missing': total_missing,
            'instance_count': len(instance_ids)
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"Error capturing patch state: {error_info}")
        return error_info
    except Exception as e:
        logger.error(f"Error capturing patch state: {e}")
        return classify_error(e)


@tool
def verify_cve_remediation(
    cve_ids: List[str],
    instance_ids: List[str],
    max_wait_minutes: int = 0
) -> dict:
    """Check CVE remediation status in Inspector. Non-blocking -- returns current state immediately.

    Args:
        cve_ids: CVE IDs to verify
        instance_ids: EC2 instance IDs that were patched
        max_wait_minutes: Ignored (kept for compatibility). Always queries immediately.

    Returns: {cve_status, summary, note}
    """
    try:
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error

        if not cve_ids:
            return {"error": "cve_ids list is empty", "category": "ABORT", "retryable": False}

        # Query immediately -- Inspector re-evaluation takes 5-15min asynchronously
        waited = 0
        logger.info(f"[TOOL:verify_cve_remediation] cve_ids={cve_ids[:5]} instances={len(instance_ids)} (non-blocking)")

        # Build instance ID set for fast lookup
        instance_set = set(instance_ids)

        # Query Inspector for each CVE -- check both ACTIVE and CLOSED findings
        cve_status: Dict[str, Dict] = {}

        for cve_id in cve_ids:
            active_instances = []
            closed_instances = []

            # Check ACTIVE findings (still vulnerable)
            try:
                active_resp = _inspector().list_findings(
                    filterCriteria={
                        'vulnerabilityId': [{'comparison': 'EQUALS', 'value': cve_id}],
                        'findingStatus': [{'comparison': 'EQUALS', 'value': 'ACTIVE'}],
                    },
                    maxResults=100
                )
                for finding in active_resp.get('findings', []):
                    for resource in finding.get('resources', []):
                        rid = resource.get('id', '')
                        if rid in instance_set:
                            active_instances.append(rid)
            except Exception as e:
                logger.warning(f"[CVE_VERIFY] Inspector query failed for {cve_id} (ACTIVE): {e}")

            # Check CLOSED findings (remediated)
            try:
                closed_resp = _inspector().list_findings(
                    filterCriteria={
                        'vulnerabilityId': [{'comparison': 'EQUALS', 'value': cve_id}],
                        'findingStatus': [{'comparison': 'EQUALS', 'value': 'CLOSED'}],
                    },
                    maxResults=100
                )
                for finding in closed_resp.get('findings', []):
                    for resource in finding.get('resources', []):
                        rid = resource.get('id', '')
                        if rid in instance_set:
                            closed_instances.append(rid)
            except Exception as e:
                logger.warning(f"[CVE_VERIFY] Inspector query failed for {cve_id} (CLOSED): {e}")

            # Deduplicate
            active_instances = list(set(active_instances))
            closed_instances = list(set(closed_instances))

            # Instances with no finding at all (never appeared in Inspector for this CVE)
            found_instances = set(active_instances) | set(closed_instances)
            not_found_instances = [i for i in instance_ids if i not in found_instances]

            total = len(instance_ids)
            remediated_count = len(closed_instances) + len(not_found_instances)  # not found = was never vulnerable or already fixed

            if not active_instances and not closed_instances:
                status = 'NOT_FOUND'  # Inspector has no record -- may not have scanned yet
            elif not active_instances:
                status = 'REMEDIATED'
            elif not closed_instances and not not_found_instances:
                status = 'STILL_ACTIVE'
            else:
                status = 'PARTIALLY_REMEDIATED'

            cve_status[cve_id] = {
                'status': status,
                'remediated_on': closed_instances + not_found_instances,
                'still_active_on': active_instances,
                'remediation_rate': f"{remediated_count}/{total} instances",
            }

        # Build summary
        statuses = [v['status'] for v in cve_status.values()]
        summary = {
            'total_cves': len(cve_ids),
            'fully_remediated': statuses.count('REMEDIATED'),
            'partially_remediated': statuses.count('PARTIALLY_REMEDIATED'),
            'still_active': statuses.count('STILL_ACTIVE'),
            'not_found_in_inspector': statuses.count('NOT_FOUND'),
        }

        logger.info(f"[CVE_VERIFY] Results: {summary}")

        # Check if pending reboots explain active CVEs
        reboot_note = None
        if summary['still_active'] > 0:
            try:
                patch_states = _ssm().describe_instance_patch_states(InstanceIds=instance_ids)
                pending_reboot = sum(
                    1 for s in patch_states.get('InstancePatchStates', [])
                    if s.get('InstalledPendingRebootCount', 0) > 0
                )
                if pending_reboot > 0:
                    reboot_note = (
                        f"{pending_reboot} instance(s) have patches installed but pending reboot. "
                        "CVEs may remain ACTIVE in Inspector until instances are rebooted."
                    )
            except Exception:
                pass

        return {
            'cve_status': cve_status,
            'summary': summary,
            'waited_minutes': waited,
            'reboot_note': reboot_note,
            'note': (
                'Inspector re-evaluates findings 5-15 minutes after patching. '
                'STILL_ACTIVE or NOT_FOUND results may update shortly. '
                'Check again in a few minutes if CVEs still show as active.'
            )
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"[CVE_VERIFY] Error: {error_info}")
        return error_info
    except Exception as e:
        logger.error(f"[CVE_VERIFY] Error: {e}")
        return classify_error(e)


@tool
def query_compliance_reports(
    days_back: int = 7,
    severity: Optional[str] = None,
    environment: Optional[str] = None,
    sla_breaches_only: bool = False,
    limit: int = 50
) -> dict:
    """Query compliance reports from S3 with filtering and statistics.

    Args:
        days_back: Number of days to look back (default: 7)
        severity: Filter by severity (CRITICAL, HIGH, MEDIUM, LOW)
        environment: Filter by environment (dev, staging, prod)
        sla_breaches_only: Only return SLA breaches (default: False)
        limit: Maximum number of reports to return (default: 50)

    Returns:
        dict: {'reports': list, 'statistics': dict, 'total_count': int}
    """
    try:
        logger.info(f"[TOOL:query_compliance_reports] days_back={days_back} severity={severity} env={environment} sla_breaches_only={sla_breaches_only}")

        bucket_name = _get_compliance_bucket_name()

        try:
            _s3().head_bucket(Bucket=bucket_name)
        except Exception:
            return {
                "reports": [],
                "total_count": 0,
                "message": "No compliance reports found. Generate reports first."
            }

        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=days_back)

        # Fetch ALL reports once (unfiltered), then apply filters in-memory.
        # This ensures the response always includes total context ("0 breaches
        # out of 4 total reports") rather than "no reports found."
        all_reports = _fetch_reports_from_s3(_s3(), bucket_name, start_date, end_date,
                                             None, None, False)

        # Apply filters in-memory for the display list
        filtered_reports = [r for r in all_reports
                           if _matches_filters(r, severity, environment, sla_breaches_only)]

        # Sort by date (most recent first)
        filtered_reports.sort(key=lambda x: x['generated_at'], reverse=True)

        # Calculate statistics from ALL reports (full picture for context)
        stats = _calculate_report_statistics(all_reports)

        # Apply limit to filtered
        limited_reports = filtered_reports[:limit]

        _s_met = stats.get('total_reports', 0) - stats.get('sla_breaches', 0) - stats.get('sla_unknown', 0)
        _s_summary = f"{len(all_reports)} reports in {start_date.date()} to {end_date.date()}. {_s_met} met SLA, {stats.get('sla_breaches', 0)} breached." if all_reports else "No reports found."

        return {
            'summary': _s_summary,
            'reports': limited_reports,
            'total_count': len(all_reports),
            'filtered_count': len(filtered_reports),
            'showing': len(limited_reports),
            'date_range': f"{start_date.date()} to {end_date.date()}",
            'statistics': stats,
            'filters_applied': {
                'severity': severity,
                'environment': environment,
                'sla_breaches_only': sla_breaches_only,
                'limit': limit
            },
            'next_action': "Present the compliance overview using the compliance_overview response template.",
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"Error querying compliance reports: {error_info}")
        error_info["reports"] = []
        error_info["total_count"] = 0
        return error_info
    except Exception as e:
        logger.error(f"Error querying compliance reports: {e}")
        result = classify_error(e)
        result["reports"] = []
        result["total_count"] = 0
        return result
