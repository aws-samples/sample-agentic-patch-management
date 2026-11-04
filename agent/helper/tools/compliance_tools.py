"""Compliance and reporting tools: patch state capture, CVE verification, report queries."""

from strands import tool

from helper import forecast, verification
from helper.display import compliance_card
from typing import Optional, List

from . import _shared
from ._shared import (
    logger, classify_error,
    ClientError,
    datetime, timedelta, timezone,
    get_client,
    _s3, _validate_instance_ids,
    _normalize_environment,
    _get_compliance_bucket_name,
)

# Threads for reading compliance report metadata.
_REPORT_WORKERS = 10
# One query reads at most this many reports, newest first.
_REPORT_READ_MAX = 1000


class _Reports(list):
    """Reports read for a query; truncated when the range held more than _REPORT_READ_MAX."""
    truncated = False


# ============================================================================
# LOCAL HELPERS (only used by tools in this module)
# ============================================================================


def _fetch_reports_from_s3(s3_client, bucket_name: str, start_date: datetime,
                           end_date: datetime, severity: Optional[str],
                           environment: Optional[str], sla_breaches_only: bool) -> list:
    """Fetch and filter reports from S3 with optimized month-prefix queries."""
    from concurrent.futures import as_completed
    from helper.concurrency import ContextThreadPoolExecutor as ThreadPoolExecutor

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
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "NoSuchBucket"):
                continue
            raise  # AccessDenied and the like: an error, not "no reports"

    out = _Reports()
    if not objects_to_check:
        return out
    if len(objects_to_check) > _REPORT_READ_MAX:
        objects_to_check.sort(key=lambda o: o["LastModified"], reverse=True)
        objects_to_check = objects_to_check[:_REPORT_READ_MAX]
        out.truncated = True

    # Parallel head_object calls (max 10 concurrent)
    def fetch_metadata(obj):
        return _parse_report_metadata(s3_client, bucket_name, obj)

    with ThreadPoolExecutor(max_workers=_REPORT_WORKERS) as executor:
        futures = {executor.submit(fetch_metadata, obj): obj for obj in objects_to_check}
        for future in as_completed(futures):
            obj = futures[future]
            try:
                report = future.result()
            except Exception as e:
                logger.warning(f"[TOOL:_fetch_reports_from_s3] Worker failed for {obj.get('Key', 'unknown')}: {e}")
                continue
            if report and _matches_filters(report, severity, environment, sla_breaches_only):
                out.append(report)

    return out


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
            'reboot_pending': int(metadata['reboot-pending']) if str(metadata.get('reboot-pending', '')).isdigit() else None,
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
    """Snapshot of patch counts per instance, for a before-and-after comparison.

    Reads the last scan result; it doesn't scan. Use when the operator wants a before or after
    record for a patch or rollback.

    Args:
        instance_ids: Instances to snapshot.
    """
    # Reads DescribeInstancePatchStates, which only changes after a scan or install.
    # Snapshots flow to the UI reconciler for the before/after delta in reports.
    try:
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error

        logger.info(f"[TOOL:capture_patch_state] instances={len(instance_ids)} instance_ids={instance_ids[:5]}")

        # Group by (account, region) for cross-region support
        by_location = _shared._locate_instances(instance_ids)

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

        # Instances with no patch state are marked as such, not given zero
        # counts: zero would read as "fully patched" in a before/after compare.
        no_data = [iid for iid in instance_ids if iid not in snapshot]
        for iid in no_data:
            snapshot[iid] = {'no_data': True, 'missing_count': None, 'installed_count': None,
                             'security_non_compliant': None, 'failed_count': None}

        logger.info(f"[TOOL:capture_patch_state] RESULT: instances={len(snapshot)} total_missing={total_missing} "
                     f"total_installed={total_installed} no_data={len(no_data)}")

        return {
            'snapshot': snapshot,
            'no_data_instances': no_data,
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
def verify_cve_remediation(cve_ids: List[str], instance_ids: List[str]) -> dict:
    """Whether Inspector has closed CVEs on instances after patching, and why not where it hasn't.

    Use after a patch run to confirm the fix. Reads current state and returns at once.

    Args:
        cve_ids: CVEs to check.
        instance_ids: Instances that were patched.
    """
    # Each instance is checked in its own region (helper.verification). "No
    # finding" is never counted as fixed; open findings get a cause.
    try:
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error
        if not cve_ids:
            return {"error": "cve_ids list is empty", "category": "ABORT", "retryable": False}
        logger.info(f"[CVE_VERIFY] cve_ids={cve_ids[:5]} instances={len(instance_ids)}")
        results = [verification.verify(cve, instance_ids) for cve in cve_ids[:10]]
        for r in results:
            logger.info(f"[CVE_VERIFY] {r['cve_id']}: {r['counts']}")
        out = {
            "cve_status": {r["cve_id"]: {"counts": r["counts"], "instances": r["instances"]} for r in results},
            "next_action": ("Say per CVE how many instances are fixed. For open findings give the cause: "
                            "pending_reboot needs a reboot, awaiting_rescan clears when Inspector rescans "
                            "(check again later), fix_not_installed means it needs patching, "
                            "no_fix_available means wait for a fix. no_finding is not proof of a fix. "
                            "terminated instances no longer exist, so leave them out of the fixed count; unlocated "
                            "ones weren't found where Patchy looked, so say they couldn't be checked, not that they're gone."),
        }
        if len(results) == 1:
            out["display"] = verification.verification_card(results[0])
        return out
    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"[CVE_VERIFY] Error: {error_info}")
        return error_info
    except Exception as e:
        logger.error(f"[CVE_VERIFY] Error: {e}")
        return classify_error(e)


@tool
def forecast_sla_misses(environment: str) -> dict:
    """Which findings in an environment scheduled patching will fix after their SLA deadline, and why.

    Use for "what will miss its SLA", "are we on track", or deadline risk. Checks each finding's
    deadline against the next scheduled install and whether the baseline approves the fix.

    Args:
        environment: dev, staging or prod.
    """
    try:
        env = _normalize_environment(environment)
        result = forecast.forecast(env)
        logger.info(f"[FORECAST] {env}: {result['will_miss']} CVE(s) at risk of {len(result['cves'])}")
        out = {"environment": env, "will_miss": result["will_miss"], "cves_assessed": len(result["cves"]),
               "cves": result["cves"][:15],
               "next_action": ("Lead with how many CVEs will miss their SLA (will_miss) out of cves_assessed, and "
                               "the main reason. Offer to plan a patch for the most urgent one that has "
                               "fix_available true; a CVE without a fix can't be patched.")}
        unassessed = sum(g["counts"].get("unknown", 0) for g in result["cves"])
        if unassessed:
            out["findings_not_assessed"] = unassessed
            out["next_action"] += (f" Say that {unassessed} finding(s) couldn't be assessed, so those instances "
                                   "may also miss; don't say the misses cover every affected instance.")
        if len(result["cves"]) > 15:
            out["cves_not_listed"] = len(result["cves"]) - 15  # the card and these counts still cover them
        if not result.get("complete", True):
            out["incomplete"] = "Some Regions' findings couldn't be read, so other CVEs may also miss."
        elif result.get("cut_off"):
            out["incomplete"] = (f"Only the {result.get('findings_assessed')} most severe findings were assessed, "
                                 "so less severe CVEs may also miss.")
        out["display"] = forecast.forecast_card(result)
        return out
    except ClientError as e:
        return classify_error(e)
    except Exception as e:
        logger.error(f"[FORECAST] Error: {e}")
        return classify_error(e)


# Reports the model sees; statistics and the card cover the full set.
MODEL_REPORT_ROWS = 20
_REPORT_NOISE = ('s3_key',)


def _reports_for_model(result: dict) -> dict:
    """Most recent reports without storage keys or empty attribution fields."""
    reports = result.get('reports') or []
    slim = []
    for r in reports[:MODEL_REPORT_ROWS]:
        slim.append({k: v for k, v in r.items()
                     if k not in _REPORT_NOISE and v not in ('unknown', [], None) or k == 'sla_met'})
    out = {**result, 'reports': slim, 'showing': len(slim)}
    if len(reports) > len(slim):
        out['reports_omitted'] = len(reports) - len(slim)
    return out


@tool
def query_compliance_reports(
    days_back: int = 7,
    severity: Optional[str] = None,
    environment: Optional[str] = None,
    sla_breaches_only: bool = False,
    limit: int = 50
) -> dict:
    """History of past patch runs and their SLA outcomes, from stored reports.

    Use for past activity, SLA breaches and audit questions. For current state use
    get_patch_compliance.

    Args:
        days_back: Days to look back (default 7).
        severity: CRITICAL, HIGH, MEDIUM or LOW.
        environment: dev, staging or prod.
        sla_breaches_only: Only runs that missed their SLA.
        limit: Maximum reports (default 50).
    """
    try:
        logger.info(f"[TOOL:query_compliance_reports] days_back={days_back} severity={severity} env={environment} sla_breaches_only={sla_breaches_only}")

        bucket_name = _get_compliance_bucket_name()

        try:
            _s3().head_bucket(Bucket=bucket_name)
        except ClientError as e:
            # Only a missing bucket means no reports; denied or throttled is an error (2 Nov review, M10).
            if e.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket", "NotFound"):
                raise
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

        truncated = getattr(all_reports, 'truncated', False)
        if truncated:
            _s_summary += (f" Only the newest {_REPORT_READ_MAX} reports in the range were read;"
                           " ask for fewer days for complete figures.")
        result = {
            'summary': _s_summary,
            'reports': limited_reports,
            'truncated': truncated,
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
            'next_action': "Summarise the reports: totals, SLA breaches by severity and environment, and two or three actions from the numbers.",
        }
        result['display'] = compliance_card(result)
        return _reports_for_model(result)

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
