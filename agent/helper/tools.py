"""Specific tools for patch automation"""

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import contextvars
import functools
import json
import logging
import re
import time
import os
from strands import tool
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, timezone
from croniter import croniter
from helper.error_handling import classify_error

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Region from environment variable (default: us-east-1)
AWS_REGION = os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))

# Per-request operator identity — safe for concurrent async requests
_current_operator: contextvars.ContextVar[str] = contextvars.ContextVar('_current_operator', default='unknown')


def set_operator(identity: str) -> None:
    """Set the operator identity for the current request context."""
    _current_operator.set(identity)


def get_operator() -> str:
    """Get the operator identity for the current request context."""
    return _current_operator.get()

# Instance ID validation pattern
INSTANCE_ID_PATTERN = re.compile(r'^i-[0-9a-f]{8,17}$')


def _validate_instance_ids(instance_ids: List[str]) -> Optional[dict]:
    """Returns error dict if any ID is invalid, None if all valid."""
    if not instance_ids:
        return {
            "error": "instance_ids list is empty",
            "error_code": "InvalidParameter",
            "category": "ABORT",
            "retryable": False
        }
    for iid in instance_ids:
        if not INSTANCE_ID_PATTERN.match(iid):
            return {
                "error": f"Invalid instance ID: {iid}",
                "error_code": "InvalidInstanceId",
                "category": "ABORT",
                "retryable": False
            }
    return None

# AWS client configuration with retry logic and timeouts
aws_config = Config(
    retries={
        'max_attempts': 3,
        'mode': 'adaptive'  # Adaptive retry mode for better handling of throttling
    },
    read_timeout=180,
    connect_timeout=10
)

# Initialize AWS clients with retry configuration
inspector_client = boto3.client('inspector2', region_name=AWS_REGION, config=aws_config)
ssm_client = boto3.client('ssm', region_name=AWS_REGION, config=aws_config)
ec2_client = boto3.client('ec2', region_name=AWS_REGION, config=aws_config)
cloudwatch_client = boto3.client('cloudwatch', region_name=AWS_REGION, config=aws_config)


# ============================================================================
# PRIVATE HELPER FUNCTIONS
# ============================================================================

@functools.lru_cache(maxsize=1)
def _load_config() -> dict:
    """Load and cache config.json from disk. Called once per process."""
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config', 'config.json')
    with open(config_path, 'r') as f:
        return json.load(f)

def _get_compliance_bucket_name() -> str:
    """Get S3 bucket name for compliance reports."""
    sts_client = boto3.client('sts', region_name=AWS_REGION, config=aws_config)
    account_id = sts_client.get_caller_identity()['Account']
    return f'patch-compliance-reports-{account_id}'


def _get_date_prefix(date: datetime) -> str:
    """Get S3 prefix for a given date."""
    return f"{date.year}/{date.month:02d}/{date.day:02d}/"


def _calculate_sla_requirement(frameworks: List[str], severity: str) -> Optional[dict]:
    """
    Helper function to calculate SLA requirement based on compliance frameworks.
    Returns strictest (minimum) SLA if multiple frameworks apply.
    
    Args:
        frameworks: List of compliance framework names (e.g., ['PCI-DSS', 'SOC2'])
        severity: Vulnerability severity (CRITICAL, HIGH, MEDIUM, LOW)
    
    Returns:
        dict with {sla_hours, source, frameworks, severity} or None if calculation fails
    """
    try:
        logger.info(f"Calculating SLA for frameworks={frameworks}, severity={severity}")
        sla_policy = _load_config()
        
        sla_hours = None
        source_framework = None
        
        # Check each framework for SLA, use strictest (minimum)
        if frameworks:
            for framework in frameworks:
                if framework in sla_policy.get('frameworks', {}):
                    framework_sla = sla_policy['frameworks'][framework].get(severity.upper())
                    logger.info(f"Framework {framework} has SLA: {framework_sla} hours for {severity}")
                    if framework_sla and (sla_hours is None or framework_sla < sla_hours):
                        sla_hours = framework_sla
                        source_framework = framework
        
        # Fall back to default if no framework SLA found
        if sla_hours is None:
            sla_hours = sla_policy.get('default', {}).get(severity.upper(), 24)
            source_framework = "default"
            logger.info(f"Using default SLA: {sla_hours} hours for {severity}")
        
        result = {
            "sla_hours": sla_hours,
            "frameworks": frameworks,
            "source": source_framework,
            "severity": severity.upper()
        }
        logger.info(f"SLA calculation result: {result}")
        return result
    except Exception as e:
        logger.error(f"Could not calculate SLA: {e}", exc_info=True)
        return None

def _normalize_environment(user_input: str) -> str:
    """Map user input to canonical environment tag value."""
    config = _load_config()
    
    aliases = config.get('environment_aliases', {})
    user_lower = user_input.lower()
    
    for canonical, alias_list in aliases.items():
        if user_lower in alias_list:
            return canonical
    
    return user_input


def _unwrap_patch_state(state: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Unwrap capture_patch_state output if needed.
    
    capture_patch_state returns {'snapshot': {iid: {...}}, 'timestamp': ..., ...}.
    generate_compliance_report and verify_rollback expect {iid: {...}}.
    
    The LLM agent may pass either format. This handles both:
    - If 'snapshot' key exists and its value is a dict → return the snapshot
    - Otherwise assume it's already the flat {iid: {...}} format → return as-is
    """
    if state is None:
        return None
    if 'snapshot' in state and isinstance(state['snapshot'], dict):
        return state['snapshot']
    return state


# ============================================================================
# PUBLIC TOOLS
# ============================================================================

# --- Vulnerability & Discovery ---

@tool
def get_vulnerability_findings(cve_id: Optional[str] = None, 
                               severity: Optional[str] = None,
                               environment: Optional[str] = None,
                               limit: int = 50) -> dict:
    """Query AWS Inspector for vulnerability findings.
    
    Args:
        cve_id: Specific CVE ID to search for (e.g., 'CVE-2024-1234')
        severity: Filter by severity (CRITICAL, HIGH, MEDIUM, LOW)
        environment: Filter by environment tag (dev, staging, prod)
        limit: Maximum number of findings to return (default: 50)
    
    Returns:
        dict: {'findings': list, 'total_count': int}
    """
    try:
        # Require at least one filter to prevent unbounded queries
        if not any([cve_id, severity, environment]):
            return {
                "error": "Must specify at least one filter: cve_id, severity, or environment",
                "findings": [],
                "total_count": 0
            }
        
        filters = {
            'findingStatus': [{'comparison': 'EQUALS', 'value': 'ACTIVE'}]
        }
        if cve_id:
            filters['vulnerabilityId'] = [{'comparison': 'EQUALS', 'value': cve_id}]
        if severity:
            filters['severity'] = [{'comparison': 'EQUALS', 'value': severity}]
        if environment:
            env_value = _normalize_environment(environment)
            filters['resourceTags'] = [{'comparison': 'EQUALS', 'key': 'Environment', 'value': env_value}]
        
        # Paginate through all findings for accurate counts
        findings = []
        total_count = 0
        next_token = None

        while True:
            kwargs = {
                'filterCriteria': filters,
                'sortCriteria': {
                    'field': 'SEVERITY',
                    'sortOrder': 'DESC'
                },
                'maxResults': 100,  # Inspector API max per page
            }
            if next_token:
                kwargs['nextToken'] = next_token

            response = inspector_client.list_findings(**kwargs)

            for finding in response.get('findings', []):
                total_count += 1
                # Only keep up to `limit` findings for the response payload
                if len(findings) < limit:
                    vuln_details = finding.get('packageVulnerabilityDetails', {})
                    findings.append({
                        'cve_id': vuln_details.get('vulnerabilityId'),
                        'severity': finding.get('severity'),
                        'cvss_score': vuln_details.get('cvss', [{}])[0].get('baseScore'),
                        'title': finding.get('title'),
                        'description': finding.get('description'),
                        'affected_resources': [r.get('id') for r in finding.get('resources', [])],
                        'status': finding.get('status'),
                        'fix_available': finding.get('fixAvailable')
                    })

            next_token = response.get('nextToken')
            if not next_token:
                break

        return {
            "findings": findings,
            "total_count": total_count
        }
    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"Error getting vulnerability findings: {error_info}")
        error_info["findings"] = []
        error_info["total_count"] = 0
        return error_info
    except Exception as e:
        logger.error(f"Error getting vulnerability findings: {e}")
        result = classify_error(e)
        result["findings"] = []
        result["total_count"] = 0
        return result


# --- Patch Management ---

@tool
def get_patch_compliance(environment: str,
                        instance_ids: Optional[List[str]] = None,
                        severity: Optional[str] = None) -> dict:
    """Get patch compliance status from AWS Systems Manager.
    
    Args:
        environment: Filter by environment (dev, staging, prod) - REQUIRED
        instance_ids: Specific instance IDs to check
        severity: If provided, includes SLA calculation based on compliance frameworks
    
    Returns:
        dict: {
            'instances': list (SSM patch data — only populated after a scan has run),
            'total_count': int (instances WITH SSM patch data),
            'ec2_instance_ids': list (ALL running instances found via EC2 tags),
            'ec2_instance_count': int (total running instances in this environment),
            'unscanned_instance_ids': list (instances in EC2 but missing from SSM — optional),
            'summary': dict,
            'sla_requirement': dict (optional)
        }
    """
    try:
        instance_tags = {}
        
        # Get instance IDs by environment if specified
        if environment and not instance_ids:
            env_value = _normalize_environment(environment)
            instance_ids = []
            paginator = ec2_client.get_paginator('describe_instances')
            for page in paginator.paginate(
                Filters=[
                    {'Name': 'tag:Environment', 'Values': [env_value]},
                    {'Name': 'instance-state-name', 'Values': ['running']}
                ]
            ):
                for reservation in page['Reservations']:
                    for instance in reservation['Instances']:
                        instance_id = instance['InstanceId']
                        instance_ids.append(instance_id)
                        # Extract tags for SLA calculation
                        if severity:
                            tags = {tag['Key']: tag['Value'] for tag in instance.get('Tags', [])}
                            instance_tags[instance_id] = tags
        
        if not instance_ids:
            return {"instances": [], "total_count": 0, "ec2_instance_ids": []}
        
        # Single API call to get patch states for all instances
        response = ssm_client.describe_instance_patch_states(InstanceIds=instance_ids)
        
        instances = []
        ssm_found_ids = set()
        for state in response.get('InstancePatchStates', []):
            ssm_found_ids.add(state['InstanceId'])
            instances.append({
                'instance_id': state['InstanceId'],
                'patch_group': state.get('PatchGroup', 'N/A'),
                'installed_count': state.get('InstalledCount', 0),
                'missing_count': state.get('MissingCount', 0),
                'failed_count': state.get('FailedCount', 0),
                'security_non_compliant_count': state.get('SecurityNonCompliantCount', 0),
                'operation': state.get('Operation', 'N/A'),
                'operation_start_time': state.get('OperationStartTime', '').isoformat() if state.get('OperationStartTime') else 'N/A',
                'operation_end_time': state.get('OperationEndTime', '').isoformat() if state.get('OperationEndTime') else 'N/A',
                'reboot_option': state.get('RebootOption', 'N/A')
            })
        
        # Instances EC2 found but SSM has no patch data for (never scanned)
        unscanned_ids = [iid for iid in instance_ids if iid not in ssm_found_ids]
        
        result = {
            "instances": instances,
            "total_count": len(instances),
            "ec2_instance_ids": instance_ids,
            "ec2_instance_count": len(instance_ids),
            "summary": {
                "total_missing": sum(i['missing_count'] for i in instances),
                "total_security_non_compliant": sum(i['security_non_compliant_count'] for i in instances),
                "instances_needing_patches": sum(1 for i in instances if i['missing_count'] > 0)
            }
        }
        
        if unscanned_ids:
            result['unscanned_instance_ids'] = unscanned_ids
        
        # Calculate SLA if severity provided — use strictest across ALL instances
        if severity and instance_tags:
            all_frameworks = set()
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
def patch_dry_run(instance_ids: List[str]) -> dict:
    """Preview what AWS-RunPatchBaseline will install, WITHOUT installing anything.
    
    Runs AWS-RunPatchBaseline with Operation=Scan on target instances. This uses
    the SAME patch baseline and logic as execute_patch_operation, so the preview
    exactly matches what will be installed.
    
    After the scan, reads DescribeInstancePatches to show which patches are
    missing (will be installed) and which are already installed.
    
    Call this BEFORE execute_patch_operation so operators can review changes.
    
    Args:
        instance_ids: List of EC2 instance IDs to preview patches for
    
    Returns:
        dict: {
            'instances': [{instance_id, missing_patches: [{id, title, severity, state}], 
                          installed_count, missing_count}],
            'total_missing': int,
            'total_instances': int,
            'scan_command_id': str
        }
    """
    try:
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error

        # Run the SAME document as execute_patch_operation, but with Scan instead of Install
        response = ssm_client.send_command(
            DocumentName='AWS-RunPatchBaseline',
            InstanceIds=instance_ids,
            Parameters={
                'Operation': ['Scan']
            },
            Comment='Patch dry-run scan (preview only, no changes)'
        )
        scan_command_id = response['Command']['CommandId']

        # Wait for scan to complete (max 5 minutes — scans can take a while)
        for _ in range(30):
            time.sleep(10)
            try:
                cmd_response = ssm_client.list_command_invocations(
                    CommandId=scan_command_id, Details=False
                )
                invocations = cmd_response.get('CommandInvocations', [])
                if invocations and all(
                    inv['Status'] in ('Success', 'Failed', 'TimedOut', 'Cancelled')
                    for inv in invocations
                ):
                    break
            except Exception:
                continue

        # Now read the patch state that the scan populated
        results = []
        total_missing = 0

        for instance_id in instance_ids:
            try:
                # Check if scan succeeded for this instance
                inv_response = ssm_client.get_command_invocation(
                    CommandId=scan_command_id, InstanceId=instance_id
                )
                if inv_response['Status'] != 'Success':
                    results.append({
                        'instance_id': instance_id,
                        'status': inv_response['Status'],
                        'missing_patches': [],
                        'installed_count': 0,
                        'missing_count': 0,
                        'error': inv_response.get('StandardErrorContent', '')[:500]
                    })
                    continue

                # Get detailed patch list — missing patches are what Install would apply
                missing_patches = []
                next_token = None
                while True:
                    kwargs = {
                        'InstanceId': instance_id,
                        'Filters': [
                            {'Key': 'State', 'Values': ['Missing', 'Failed']}
                        ],
                        'MaxResults': 50
                    }
                    if next_token:
                        kwargs['NextToken'] = next_token
                    
                    patches_response = ssm_client.describe_instance_patches(**kwargs)
                    
                    for patch in patches_response.get('Patches', []):
                        missing_patches.append({
                            'id': patch.get('KBId', patch.get('Title', 'unknown')),
                            'title': patch.get('Title', 'unknown'),
                            'severity': patch.get('Severity', 'unknown'),
                            'classification': patch.get('Classification', 'unknown'),
                            'state': patch.get('State', 'unknown'),
                            'cve_ids': patch.get('CVEIds', ''),
                            'installed_time': str(patch.get('InstalledTime', ''))
                        })
                    
                    next_token = patches_response.get('NextToken')
                    if not next_token:
                        break

                # Also get the summary counts
                patch_state = ssm_client.describe_instance_patch_states(
                    InstanceIds=[instance_id]
                )
                state_data = patch_state.get('InstancePatchStates', [{}])[0] if patch_state.get('InstancePatchStates') else {}
                installed_count = state_data.get('InstalledCount', 0)
                missing_count = state_data.get('MissingCount', 0)
                failed_count = state_data.get('FailedCount', 0)
                security_nc = state_data.get('SecurityNonCompliantCount', 0)

                total_missing += missing_count

                results.append({
                    'instance_id': instance_id,
                    'status': 'Success',
                    'missing_patches': missing_patches[:50],  # Cap at 50 per instance for response size
                    'missing_patches_truncated': len(missing_patches) > 50,
                    'installed_count': installed_count,
                    'missing_count': missing_count,
                    'failed_count': failed_count,
                    'security_non_compliant': security_nc
                })

            except Exception as e:
                results.append({
                    'instance_id': instance_id,
                    'status': 'Error',
                    'missing_patches': [],
                    'installed_count': 0,
                    'missing_count': 0,
                    'error': str(e)[:200]
                })

        return {
            'instances': results,
            'total_missing': total_missing,
            'total_instances': len(instance_ids),
            'scan_command_id': scan_command_id,
            'message': (
                f'{total_missing} missing patches across {len(instance_ids)} instances will be installed'
                if total_missing > 0
                else f'All {len(instance_ids)} instances are fully patched — nothing to install'
            )
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
    maintenance_window_id: Optional[str] = None
) -> dict:
    """Execute patching based on SLA decision (SCHEDULED or EMERGENCY).
    
    Agent decides execution_mode based on SLA vs maintenance window comparison:
    - SCHEDULED: Window is within SLA deadline → register with maintenance window
    - EMERGENCY: Window exceeds SLA deadline → patch immediately
    
    Args:
        instance_ids: List of EC2 instance IDs to patch
        execution_mode: "scheduled" or "immediate" (based on agent's SLA decision)
        environment: Environment name (dev/staging/prod)
        maintenance_window_id: Required if execution_mode="scheduled"
    
    Returns:
        dict: Status and tracking information
    
    Examples:
        # SCHEDULED patching (window within SLA)
        execute_patch_operation(
            instance_ids=["i-123", "i-456"],
            execution_mode="scheduled",
            environment="prod",
            maintenance_window_id="mw-abc123"
        )
        
        # EMERGENCY patching (window exceeds SLA)
        execute_patch_operation(
            instance_ids=["i-123", "i-456"],
            execution_mode="immediate",
            environment="prod"
        )
    """
    try:
        # Validate instance IDs
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error

        # ── Dry-run gate: refuse to patch unless a recent Scan was run ──
        # patch_dry_run runs AWS-RunPatchBaseline with Operation=Scan, which
        # sets Operation='Scan' and OperationEndTime in DescribeInstancePatchStates.
        # We check that every target instance has a Scan within the last 2 hours.
        try:
            scan_check = ssm_client.describe_instance_patch_states(InstanceIds=instance_ids)
            for state in scan_check.get('InstancePatchStates', []):
                iid = state['InstanceId']
                last_op = state.get('Operation', '')
                op_end = state.get('OperationEndTime')

                if last_op != 'Scan' or not op_end:
                    return {
                        "error": f"No recent dry-run scan found for {iid}. "
                                 "Run patch_dry_run first so operators can review changes before patching.",
                        "error_code": "DryRunRequired",
                        "category": "ABORT",
                        "suggestion": "Call patch_dry_run(instance_ids) before execute_patch_operation.",
                        "retryable": False
                    }

                if op_end.tzinfo is None:
                    op_end = op_end.replace(tzinfo=timezone.utc)
                hours_since_scan = (datetime.now(timezone.utc) - op_end).total_seconds() / 3600
                if hours_since_scan > 2:
                    return {
                        "error": f"Dry-run scan for {iid} is {hours_since_scan:.1f}hr old. "
                                 "Run a fresh patch_dry_run so the preview reflects current state.",
                        "error_code": "DryRunStale",
                        "category": "ABORT",
                        "suggestion": "Call patch_dry_run(instance_ids) to refresh the scan.",
                        "retryable": False
                    }

            # Also catch instances with no patch state at all (never scanned)
            scanned_ids = {s['InstanceId'] for s in scan_check.get('InstancePatchStates', [])}
            unscanned = [iid for iid in instance_ids if iid not in scanned_ids]
            if unscanned:
                return {
                    "error": f"No patch state data for {', '.join(unscanned[:5])}. "
                             "Run patch_dry_run first to populate patch baseline data.",
                    "error_code": "DryRunRequired",
                    "category": "ABORT",
                    "suggestion": "Call patch_dry_run(instance_ids) before execute_patch_operation.",
                    "retryable": False
                }
        except ClientError as scan_err:
            # If we can't verify the dry-run, log but don't block — the scan check
            # is a safety net, not a hard dependency on a separate service.
            logger.warning(f"Could not verify dry-run scan state: {scan_err}")

        if execution_mode == "scheduled":
            # Validate maintenance_window_id is provided
            if not maintenance_window_id:
                return {
                    "error": "maintenance_window_id required for scheduled patching",
                    "suggestion": "Call get_maintenance_windows first to get window_id"
                }
            
            # Register task with maintenance window
            response = ssm_client.register_task_with_maintenance_window(
                WindowId=maintenance_window_id,
                Targets=[{
                    'Key': 'InstanceIds',
                    'Values': instance_ids
                }],
                TaskArn='AWS-RunPatchBaseline',
                TaskType='RUN_COMMAND',
                TaskInvocationParameters={
                    'RunCommand': {
                        'Parameters': {
                            'Operation': ['Install'],
                            'RebootOption': ['NoReboot']
                        }
                    }
                },
                Priority=1,
                MaxConcurrency='50%',
                MaxErrors='10%',
                Name=f'scheduled-patch-{environment}-{datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")}'
            )
            
            # Get window details
            window_details = ssm_client.get_maintenance_window(WindowId=maintenance_window_id)
            
            operator = get_operator()
            logger.info(f"PATCH_SCHEDULED: operator={operator} environment={environment} "
                        f"mode=scheduled window={window_details.get('Name')} "
                        f"instances={len(instance_ids)}")
            
            return {
                'execution_mode': 'scheduled',
                'status': 'SCHEDULED',
                'window_task_id': response['WindowTaskId'],
                'maintenance_window_id': maintenance_window_id,
                'window_name': window_details.get('Name'),
                'next_execution': window_details.get('NextExecutionTime', 'See maintenance window schedule'),
                'instance_count': len(instance_ids),
                'operator': operator,
                'message': f'Patching scheduled in maintenance window: {window_details.get("Name")}'
            }
        
        elif execution_mode == "immediate":
            # Execute immediately (EMERGENCY)
            operator = get_operator()
            response = ssm_client.send_command(
                DocumentName='AWS-RunPatchBaseline',
                InstanceIds=instance_ids,
                Parameters={
                    'Operation': ['Install'],
                    'RebootOption': ['NoReboot']
                },
                Comment=f'EMERGENCY patching for {environment} by {operator}'
            )
            
            logger.info(f"PATCH_EXECUTED: operator={operator} environment={environment} "
                        f"mode=immediate instances={len(instance_ids)} "
                        f"command_id={response['Command']['CommandId']}")
            
            return {
                'execution_mode': 'immediate',
                'status': 'EXECUTING',
                'command_id': response['Command']['CommandId'],
                'instance_count': len(instance_ids),
                'operator': operator,
                'message': f'Emergency patching initiated for {len(instance_ids)} instances'
            }
        
        else:
            return {
                "error": f"Invalid execution_mode: {execution_mode}. Must be 'scheduled' or 'immediate'",
                "error_code": "InvalidParameter",
                "category": "ABORT",
                "suggestion": "Use execution_mode='scheduled' or execution_mode='immediate'",
                "retryable": False
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
                response = ssm_client.get_command_invocation(
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
                results.append(future.result())
        
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
        
        return {
            'command_id': command_id,
            'status': overall_status,
            'results': results,
            'summary': summary
        }
    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"Error getting command status: {error_info}")
        error_info["command_id"] = command_id
        return error_info
    except Exception as e:
        logger.error(f"Error getting command status: {e}")
        return classify_error(e)


# --- Maintenance Windows ---

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
        response = ssm_client.create_maintenance_window(
            Name=name,
            Schedule=schedule,
            Duration=duration,
            Cutoff=1,
            AllowUnassociatedTargets=False
        )
        
        window_id = response['WindowId']
        
        # Register targets
        ssm_client.register_target_with_maintenance_window(
            WindowId=window_id,
            ResourceType='INSTANCE',
            Targets=[{
                'Key': 'tag:Environment',
                'Values': [target_environment]
            }]
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


# --- Health & Monitoring ---

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

        # Batch: Check SSM connectivity for all instances in single API call
        ssm_info = ssm_client.describe_instance_information(
            Filters=[{'Key': 'InstanceIds', 'Values': instance_ids}]
        )
        
        # Create lookup map for fast access
        instance_info_map = {
            info['InstanceId']: info 
            for info in ssm_info.get('InstanceInformationList', [])
        }
        
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

        # Query all alarms in ALARM state with pagination
        alarms_in_alarm = []
        next_token = None
        
        while True:
            params = {'StateValue': 'ALARM', 'MaxRecords': 100}
            if next_token:
                params['NextToken'] = next_token
            
            response = cloudwatch_client.describe_alarms(**params)
            alarms_in_alarm.extend(response.get('MetricAlarms', []))
            
            next_token = response.get('NextToken')
            if not next_token:
                break
        
        alarm_results = []
        for instance_id in instance_ids:
            instance_alarms = []
            
            for alarm in alarms_in_alarm:
                # Check if alarm has InstanceId dimension
                dimensions = alarm.get('Dimensions', [])
                has_instance = any(
                    d['Name'] == 'InstanceId' and d['Value'] == instance_id 
                    for d in dimensions
                )
                
                if has_instance:
                    # Apply optional name filter
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
        
        result = {
            'results': alarm_results,
            'total_instances': len(instance_ids),
            'unhealthy_instances': unhealthy_count,
            'total_alarms_firing': total_alarms,
            'recommendation': 'HEALTHY' if unhealthy_count == 0 else 'ROLLBACK_RECOMMENDED',
            'message': (
                f'{unhealthy_count} instances have alarms firing ({total_alarms} total alarms)' 
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
        import time
        
        # Validate instance IDs
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error
        
        # Poll command status until complete (max 10 minutes)
        max_attempts = 60  # 10 min with 10s intervals
        attempt = 0
        
        while attempt < max_attempts:
            status_result = get_command_status(command_id, instance_ids)
            
            if status_result.get('status') == 'Success':
                # Wait 2 minutes for CloudWatch alarm evaluation
                logger.info("Patching complete. Waiting 2 minutes for alarm evaluation...")
                time.sleep(120)
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
                time.sleep(10)
        
        if attempt >= max_attempts:
            return {
                'status': 'TIMEOUT',
                'recommendation': 'INVESTIGATE',
                'reason': 'Patch command did not complete within 10 minutes',
                'details': status_result
            }
        
        # Run health checks
        ssm_health = check_instance_health(instance_ids)
        alarm_health = check_cloudwatch_alarms(instance_ids, alarm_pattern)
        
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


@tool
def rollback_patches(instance_ids: List[str]) -> dict:
    """Rollback patches on EC2 instances using yum history.
    
    Args:
        instance_ids: List of EC2 instance IDs to rollback patches on
    
    Returns:
        dict: {'command_id': str, 'status': str, 'instance_count': int}
    """
    try:
        # Validate instance IDs
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error

        # Use yum history to rollback the last transaction
        rollback_script = """
#!/bin/bash
# Get the last yum transaction ID (skip header and separator lines)
LAST_TRANSACTION=$(yum history list | grep -E '^[[:space:]]*[0-9]+' | head -1 | awk '{print $1}')

if [ -z "$LAST_TRANSACTION" ]; then
    echo "ERROR: No yum history found"
    echo "SUGGESTION: Rollback not possible via yum history. Consider:"
    echo "  1. Restore from AMI/snapshot if available"
    echo "  2. Manually downgrade packages: yum downgrade <package>"
    echo "  3. Redeploy instance from known-good configuration"
    exit 1
fi

echo "Rolling back transaction ID: $LAST_TRANSACTION"
yum history undo $LAST_TRANSACTION -y

if [ $? -eq 0 ]; then
    echo "SUCCESS: Rollback completed for transaction $LAST_TRANSACTION"
    exit 0
else
    echo "ERROR: Rollback failed for transaction $LAST_TRANSACTION"
    echo "SUGGESTION: Check /var/log/yum.log for details"
    echo "  Manual rollback may be required"
    exit 1
fi
"""
        
        response = ssm_client.send_command(
            DocumentName='AWS-RunShellScript',
            InstanceIds=instance_ids,
            Parameters={
                'commands': [rollback_script]
            },
            Comment='Automated patch rollback'
        )
        
        return {
            'status': 'ROLLBACK_INITIATED',
            'command_id': response['Command']['CommandId'],
            'instance_count': len(instance_ids),
            'instance_ids': instance_ids,
            'message': 'Rollback command sent. Use get_command_status to verify completion. If rollback fails, check command output for manual recovery suggestions.'
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
                    pre_patch_state: Optional[Dict[str, Any]] = None) -> dict:
    """Verify that a rollback succeeded by re-scanning and comparing against pre-patch state.
    
    Call this after rollback_patches completes. Performs:
    1. Waits for rollback command to complete
    2. Re-runs AWS-RunPatchBaseline Scan to get current patch state
    3. Compares current state against pre_patch_state (from capture_patch_state before patching)
    4. Runs SSM health + CloudWatch alarm checks
    
    This uses the same scan mechanism as patch_dry_run, so the comparison is
    aligned with how patches were originally assessed and installed.
    
    Args:
        command_id: SSM command ID from rollback_patches
        instance_ids: List of instance IDs that were rolled back
        pre_patch_state: Snapshot from capture_patch_state taken BEFORE patching.
            Format: {"i-xxx": {"missing_count": 12, "installed_count": 45, ...}}
            If provided, verifies patch counts returned to pre-patch levels.
    
    Returns:
        dict: {
            'status': 'VERIFIED' | 'PARTIAL' | 'FAILED',
            'instances': [{instance_id, command_status, patch_state_match, health_ok}],
            'recommendation': str
        }
    """
    try:
        validation_error = _validate_instance_ids(instance_ids)
        if validation_error:
            return validation_error

        # Step 1: Wait for rollback command to complete (max 5 min)
        status_result = None
        for _ in range(30):
            time.sleep(10)
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
        if status_result.get('status') == 'Success' and pre_patch_state:
            try:
                # Run the same scan as patch_dry_run
                scan_response = ssm_client.send_command(
                    DocumentName='AWS-RunPatchBaseline',
                    InstanceIds=instance_ids,
                    Parameters={'Operation': ['Scan']},
                    Comment='Post-rollback verification scan'
                )
                scan_cmd_id = scan_response['Command']['CommandId']

                # Wait for scan (max 3 min)
                for _ in range(18):
                    time.sleep(10)
                    try:
                        cmd_resp = ssm_client.list_command_invocations(
                            CommandId=scan_cmd_id, Details=False
                        )
                        invs = cmd_resp.get('CommandInvocations', [])
                        if invs and all(i['Status'] in ('Success', 'Failed') for i in invs):
                            break
                    except Exception:
                        continue

                # Compare current patch state against pre-patch snapshot
                current_state = ssm_client.describe_instance_patch_states(
                    InstanceIds=instance_ids
                )
                current_map = {
                    s['InstanceId']: s
                    for s in current_state.get('InstancePatchStates', [])
                }

                for iid in instance_ids:
                    before = pre_patch_state.get(iid, {})
                    after_raw = current_map.get(iid, {})
                    after = {
                        'missing_count': after_raw.get('MissingCount', 0),
                        'installed_count': after_raw.get('InstalledCount', 0),
                    }
                    before_missing = before.get('missing_count', 0)
                    after_missing = after.get('missing_count', 0)

                    # Rollback is verified if missing count returned to pre-patch level
                    # (or close — exact match may not happen due to new patches released)
                    reverted = after_missing >= before_missing

                    patch_state_comparison[iid] = {
                        'before_missing': before_missing,
                        'after_missing': after_missing,
                        'reverted': reverted,
                        'before_installed': before.get('installed_count', 0),
                        'after_installed': after.get('installed_count', 0)
                    }

            except Exception as e:
                logger.warning(f"Post-rollback scan failed: {e}")

        # Step 3: Health checks
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
        logger.error(f"Error verifying rollback: {error_info}")
        error_info["recommendation"] = "INVESTIGATE"
        return error_info
    except Exception as e:
        logger.error(f"Error verifying rollback: {e}")
        result = classify_error(e)
        result["recommendation"] = "INVESTIGATE"
        return result


# --- Fleet Impact Assessment ---

@tool
def assess_fleet_impact(cve_id: str) -> dict:
    """Assess fleet-wide impact of a CVE across all environments.

    Queries Inspector for the CVE across all environments in one call, groups
    affected instances by environment, cross-references maintenance windows and
    SLA policy, and generates a phased rollout recommendation.

    Use this instead of per-instance dependency checks — it gives operators the
    full picture in a single call.

    Args:
        cve_id: CVE identifier (e.g., 'CVE-2024-1234')

    Returns:
        dict: {
            'cve_id': str,
            'total_affected': int,
            'environments': [{name, instance_count, instance_ids, sla_deadline_hours,
                              next_window, hours_until_window}],
            'rollout_order': [env_names],
            'severity': str,
            'cvss_score': float,
            'fix_available': str
        }
    """
    try:
        # 1. Query Inspector for this CVE across ALL environments
        findings_result = get_vulnerability_findings(cve_id=cve_id, limit=200)
        if findings_result.get('error'):
            return findings_result

        findings = findings_result.get('findings', [])
        total_count = findings_result.get('total_count', 0)

        if total_count == 0:
            return {
                'cve_id': cve_id,
                'total_affected': 0,
                'environments': [],
                'rollout_order': [],
                'message': f'No active findings for {cve_id}'
            }

        # Extract severity/CVSS from first finding
        severity = findings[0].get('severity', 'UNKNOWN') if findings else 'UNKNOWN'
        cvss_score = findings[0].get('cvss_score') if findings else None
        fix_available = findings[0].get('fix_available', 'UNKNOWN') if findings else 'UNKNOWN'

        # 2. Collect all unique affected instance IDs
        all_instance_ids = set()
        for f in findings:
            for rid in f.get('affected_resources', []):
                if rid.startswith('i-'):
                    all_instance_ids.add(rid)

        if not all_instance_ids:
            return {
                'cve_id': cve_id,
                'total_affected': total_count,
                'environments': [],
                'rollout_order': [],
                'severity': severity,
                'cvss_score': cvss_score,
                'message': f'{total_count} findings but no EC2 instance IDs in results'
            }

        # 3. Get environment tags for all instances (single API call with pagination)
        instance_env_map = {}  # instance_id -> environment
        instance_frameworks = {}  # instance_id -> [frameworks]
        paginator = ec2_client.get_paginator('describe_instances')
        for page in paginator.paginate(InstanceIds=list(all_instance_ids)):
            for reservation in page['Reservations']:
                for inst in reservation['Instances']:
                    iid = inst['InstanceId']
                    tags = {t['Key']: t['Value'] for t in inst.get('Tags', [])}
                    instance_env_map[iid] = tags.get('Environment', 'unknown')
                    fw_str = tags.get('ComplianceFrameworks', '')
                    instance_frameworks[iid] = [f.strip() for f in fw_str.split(',') if f.strip()]

        # 4. Group by environment
        env_groups = {}  # env_name -> [instance_ids]
        for iid, env in instance_env_map.items():
            env_groups.setdefault(env, []).append(iid)

        # 5. For each environment: get SLA + maintenance window
        env_details = []
        for env_name, env_instance_ids in sorted(env_groups.items()):
            # SLA: use strictest framework across all instances in this env
            all_fw = set()
            for iid in env_instance_ids:
                all_fw.update(instance_frameworks.get(iid, []))

            sla = _calculate_sla_requirement(list(all_fw) if all_fw else [], severity)
            sla_hours = sla['sla_hours'] if sla else None
            sla_source = sla['source'] if sla else 'default'

            # Maintenance window
            try:
                mw_result = get_maintenance_windows(env_name)
                windows = mw_result.get('windows', [])
                if windows:
                    # Pick the next enabled window
                    next_win = None
                    for w in windows:
                        if w.get('enabled', True) and w.get('next_execution'):
                            next_win = w
                            break
                    if next_win:
                        next_window_name = next_win.get('name', 'unknown')
                        next_execution = next_win.get('next_execution', 'unknown')
                        # Calculate hours until window
                        try:
                            win_dt = datetime.fromisoformat(next_execution.replace('Z', '+00:00'))
                            hours_until = max(0, (win_dt - datetime.now(timezone.utc)).total_seconds() / 3600)
                            hours_until = round(hours_until, 1)
                        except Exception:
                            hours_until = None
                    else:
                        next_window_name = 'none'
                        next_execution = None
                        hours_until = None
                else:
                    next_window_name = 'none'
                    next_execution = None
                    hours_until = None
            except Exception as e:
                logger.warning(f"Could not get maintenance windows for {env_name}: {e}")
                next_window_name = 'error'
                next_execution = None
                hours_until = None

            env_details.append({
                'environment': env_name,
                'instance_count': len(env_instance_ids),
                'instance_ids': env_instance_ids,
                'sla_hours': sla_hours,
                'sla_source': sla_source,
                'next_window': next_window_name,
                'next_execution': next_execution,
                'hours_until_window': hours_until,
            })

        # 6. Generate rollout order: dev → staging → prod (standard safe order)
        env_priority = {'dev': 0, 'test': 1, 'staging': 2, 'prod': 3}
        env_details.sort(key=lambda e: env_priority.get(e['environment'], 99))
        rollout_order = [e['environment'] for e in env_details]

        return {
            'cve_id': cve_id,
            'severity': severity,
            'cvss_score': cvss_score,
            'fix_available': fix_available,
            'total_affected': len(all_instance_ids),
            'total_findings': total_count,
            'environments': env_details,
            'rollout_order': rollout_order,
            'message': f'{len(all_instance_ids)} instances affected across {len(env_details)} environments'
        }

    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"Error assessing fleet impact for {cve_id}: {error_info}")
        error_info['cve_id'] = cve_id
        return error_info
    except Exception as e:
        logger.error(f"Error assessing fleet impact for {cve_id}: {e}")
        result = classify_error(e)
        result['cve_id'] = cve_id
        return result


# --- Compliance & Reporting ---

@tool
def get_maintenance_windows(environment: str, instance_ids: list = None) -> dict:
    """Get maintenance window information and validate instance coverage.
    
    Args:
        environment: Filter by environment (dev, staging, prod) - REQUIRED
        instance_ids: Optional list of instance IDs to validate coverage
    
    Returns:
        dict: {'windows': list, 'instance_coverage': dict (optional)}
    """
    try:
        # Normalize environment name for consistent matching
        env_value = _normalize_environment(environment)
        
        # Query all maintenance windows (filter by environment later)
        response = ssm_client.describe_maintenance_windows()
        
        # Get instance tags if instance_ids provided
        instance_tags = {}
        if instance_ids:
            try:
                ec2_response = ec2_client.describe_instances(InstanceIds=instance_ids)
                for reservation in ec2_response['Reservations']:
                    for instance in reservation['Instances']:
                        instance_id = instance['InstanceId']
                        tags = {tag['Key']: tag['Value'] for tag in instance.get('Tags', [])}
                        instance_tags[instance_id] = tags
            except Exception as e:
                logger.warning(f"Could not get instance tags: {e}")
        
        windows = []
        for window_identity in response.get('WindowIdentities', []):
            window_id = window_identity['WindowId']
            
            try:
                # Get detailed window information
                window_details = ssm_client.get_maintenance_window(WindowId=window_id)
                
                # Get targets for this window
                targets_response = ssm_client.describe_maintenance_window_targets(
                    WindowId=window_id
                )
                
                # Calculate next execution time from cron schedule
                schedule = window_details.get('Schedule', '')
                next_execution = None
                if schedule:
                    try:
                        cron_expr = schedule.replace('cron(', '').replace(')', '').strip()
                        # AWS cron: minute hour day month day-of-week year
                        # Standard cron: minute hour day month day-of-week
                        parts = cron_expr.split()
                        if len(parts) == 6:
                            cron_expr = ' '.join(parts[:5])
                        
                        cron = croniter(cron_expr, datetime.now(timezone.utc))
                        next_execution = cron.get_next(datetime).isoformat() + 'Z'
                    except Exception as cron_error:
                        logger.warning(f"Could not parse cron '{schedule}': {cron_error}")
                        next_execution = "Unable to calculate"
                
                # Format targets and validate instances
                target_list = []
                instances_covered = []
                instances_not_covered = []
                
                for target in targets_response.get('Targets', []):
                    target_filters = target.get('Targets', [])
                    target_list.append({
                        'resource_type': target.get('ResourceType'),
                        'filters': target_filters
                    })
                    
                    # Validate instances against this target's filters
                    if instance_ids and instance_tags:
                        for instance_id in instance_ids:
                            tags = instance_tags.get(instance_id, {})
                            # Check if instance matches target filters
                            matches = True
                            for filter_item in target_filters:
                                key = filter_item.get('Key', '')
                                values = filter_item.get('Values', [])
                                
                                # Extract tag name from key (e.g., "tag:Environment" -> "Environment")
                                if key.startswith('tag:'):
                                    tag_name = key.replace('tag:', '')
                                    instance_tag_value = tags.get(tag_name)
                                    if instance_tag_value not in values:
                                        matches = False
                                        break
                            
                            if matches and instance_id not in instances_covered:
                                instances_covered.append(instance_id)
                            elif not matches and instance_id not in instances_not_covered:
                                instances_not_covered.append(instance_id)
                
                # Calculate coverage
                coverage_percentage = 0
                if instance_ids:
                    coverage_percentage = int((len(instances_covered) / len(instance_ids)) * 100)
                
                # Determine window environment from targets
                window_environment = 'unknown'
                for target in target_list:
                    for filter_item in target.get('filters', []):
                        if filter_item.get('Key') == 'tag:Environment':
                            values = filter_item.get('Values', [])
                            if values:
                                window_environment = values[0]
                                break
                
                # Skip if environment filter specified and doesn't match
                if env_value and window_environment != env_value:
                    continue
                
                window_info = {
                    'window_id': window_id,
                    'name': window_details.get('Name'),
                    'schedule': schedule,
                    'next_execution': next_execution,
                    'duration_hours': window_details.get('Duration'),
                    'cutoff_hours': window_details.get('Cutoff'),
                    'enabled': window_details.get('Enabled'),
                    'target_count': len(target_list),
                    'targets': target_list,
                    'environment': window_environment
                }
                
                # Add instance validation results if requested
                if instance_ids:
                    window_info['instances_covered'] = instances_covered
                    window_info['instances_not_covered'] = instances_not_covered
                    window_info['coverage_percentage'] = coverage_percentage
                
                windows.append(window_info)
                
            except Exception as window_error:
                logger.warning(f"Error processing window {window_id}: {window_error}")
                continue
        
        return {
            'windows': windows,
            'count': len(windows),
            'environment_filter': environment if environment else 'all',
            'instances_validated': len(instance_ids) if instance_ids else 0
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


# ── Patch policy association cache (session-scoped) ─────────────────
# list-associations is 1 API call regardless of fleet size.
# We cache it to avoid repeated calls within the same agent session.
_patch_assoc_cache: Optional[List[dict]] = None
_patch_assoc_cache_time: float = 0
_PATCH_ASSOC_CACHE_TTL = 300  # 5 minutes


def _get_cached_patch_associations() -> List[dict]:
    """Get all patch-related SSM associations (cached for 5 min).

    Uses list-associations (1 paginated API call) instead of per-instance
    describe-instance-associations-status (N calls). This scales to any
    fleet size — the only variable is the number of associations in the
    account (typically 10-50, not thousands).
    """
    global _patch_assoc_cache, _patch_assoc_cache_time
    now = time.time()
    if _patch_assoc_cache is not None and (now - _patch_assoc_cache_time) < _PATCH_ASSOC_CACHE_TTL:
        return _patch_assoc_cache

    all_assocs = []
    paginator = ssm_client.get_paginator('list_associations')
    for page in paginator.paginate():
        all_assocs.extend(page['Associations'])

    # Filter to patch-related associations only
    patch_docs = {'AWS-RunPatchBaseline', 'AWS-RunPatchBaselineAssociation'}
    patch_assocs = [a for a in all_assocs if a.get('Name') in patch_docs]

    _patch_assoc_cache = patch_assocs
    _patch_assoc_cache_time = now
    logger.info(f"Cached {len(patch_assocs)} patch associations (of {len(all_assocs)} total)")
    return patch_assocs


def _match_association_to_instance(assoc_targets: List[dict], instance_id: str,
                                   instance_tags: Dict[str, str]) -> bool:
    """Check if an association's targets match a specific instance.

    Handles all SSM target types:
    - InstanceIds: ['*'] → matches all
    - InstanceIds: ['i-abc', 'i-def'] → explicit list
    - tag:Key: ['Value1', 'Value2'] → tag-based targeting
    - tag-key: ['KeyName'] → instances that have the tag (any value)
    """
    if not assoc_targets:
        return False

    for target in assoc_targets:
        key = target.get('Key', '')
        values = target.get('Values', [])

        if key == 'InstanceIds':
            if values == ['*']:
                return True
            if instance_id in values:
                return True
            return False

        if key.startswith('tag:'):
            tag_name = key[4:]  # strip 'tag:' prefix
            tag_value = instance_tags.get(tag_name)
            if tag_value not in values:
                return False
            # Continue checking other targets (AND logic)

        elif key == 'tag-key':
            if not any(v in instance_tags for v in values):
                return False

        else:
            # Unknown target type — don't match
            return False

    return True


@tool
def get_patch_policy(instance_ids: List[str], environment: Optional[str] = None) -> dict:
    """Check which SSM Patch Policy associations apply to the given instances.

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

        # Get instance tags (we need these for target matching)
        instance_tags_map: Dict[str, Dict[str, str]] = {}
        try:
            resp = ec2_client.describe_instances(InstanceIds=instance_ids)
            for reservation in resp['Reservations']:
                for inst in reservation['Instances']:
                    iid = inst['InstanceId']
                    tags = {t['Key']: t['Value'] for t in inst.get('Tags', [])}
                    instance_tags_map[iid] = tags
        except ClientError as e:
            logger.warning(f"Could not get instance tags: {e}")

        # Get cached patch associations (1 API call, cached 5 min)
        patch_assocs = _get_cached_patch_associations()

        # Enrich associations with details (schedule, operation, etc.)
        enriched_assocs = []
        for pa in patch_assocs:
            assoc_id = pa['AssociationId']
            try:
                detail = ssm_client.describe_association(AssociationId=assoc_id)
                desc = detail['AssociationDescription']
                params = desc.get('Parameters', {})
                operation = 'Scan'
                for op_val in params.get('Operation', []):
                    operation = op_val

                enriched_assocs.append({
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
                enriched_assocs.append({
                    'association_id': assoc_id,
                    'name': pa.get('AssociationName', pa['Name']),
                    'document': pa['Name'],
                    'operation': 'unknown',
                    'schedule': 'unknown',
                    'targets': pa.get('Targets', []),
                    'status': 'unknown',
                })

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
            summary_parts.append(f"{len(with_install)} have Install policy")
        if with_scan_only:
            summary_parts.append(f"{len(with_scan_only)} have Scan-only policy")
        if without_policy:
            summary_parts.append(f"{len(without_policy)} have NO patch policy")
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
def capture_patch_state(instance_ids: List[str]) -> dict:
    """Capture current patch compliance state for instances (snapshot).
    
    Call this BEFORE patching to capture pre-patch state, and AFTER patching
    to capture post-patch state. Pass both snapshots to generate_compliance_report
    for before/after delta in the compliance report.
    
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

        response = ssm_client.describe_instance_patch_states(InstanceIds=instance_ids)

        snapshot = {}
        total_missing = 0
        for state in response.get('InstancePatchStates', []):
            iid = state['InstanceId']
            missing = state.get('MissingCount', 0)
            total_missing += missing
            snapshot[iid] = {
                'missing_count': missing,
                'installed_count': state.get('InstalledCount', 0),
                'security_non_compliant': state.get('SecurityNonCompliantCount', 0),
                'failed_count': state.get('FailedCount', 0)
            }

        # Include instances with no patch state data
        for iid in instance_ids:
            if iid not in snapshot:
                snapshot[iid] = {
                    'missing_count': 0,
                    'installed_count': 0,
                    'security_non_compliant': 0,
                    'failed_count': 0
                }

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
def generate_compliance_report(
    cve_id: str,
    environment: str,
    command_id: str,
    decision: str,
    severity: Optional[str] = None,
    cvss_score: Optional[float] = None,
    sla_hours: Optional[int] = None,
    sla_source: Optional[str] = None,
    pre_patch_state: Optional[Dict[str, Any]] = None,
    post_patch_state: Optional[Dict[str, Any]] = None
) -> dict:
    """Generate compliance report after patching, with optional before/after delta.
    
    Call this after patching completes (get_command_status shows Success).
    Pass data from previous tool calls when available to avoid redundant API calls.
    
    For compliance delta, pass pre_patch_state (captured before patching) and
    post_patch_state (captured after patching). Format for each:
        {"i-xxx": {"missing_count": 12, "installed_count": 45, "security_non_compliant": 3}}
    
    Args:
        cve_id: CVE identifier (e.g., 'CVE-2024-1234')
        environment: Target environment (dev, staging, prod)
        command_id: SSM command ID from execute_patch_operation
        decision: Patching decision (EMERGENCY, SCHEDULED)
        severity: Vulnerability severity (from get_vulnerability_findings)
        cvss_score: CVSS score (from get_vulnerability_findings)
        sla_hours: SLA requirement hours (from get_patch_compliance)
        sla_source: SLA source framework (from get_patch_compliance)
        pre_patch_state: Patch state before execution (from capture_patch_state)
        post_patch_state: Patch state after execution (from capture_patch_state)
    
    Returns:
        dict: {'report_id': str, 's3_location': str, 'sla_met': bool, 'delta': dict (if states provided)}
    """
    try:
        # First, get instance IDs from the command (paginate to get all)
        try:
            instance_ids = []
            next_token = None
            while True:
                kwargs = {'CommandId': command_id, 'MaxResults': 50}
                if next_token:
                    kwargs['NextToken'] = next_token
                ssm_response = ssm_client.list_command_invocations(**kwargs)
                instance_ids.extend(inv['InstanceId'] for inv in ssm_response.get('CommandInvocations', []))
                next_token = ssm_response.get('NextToken')
                if not next_token:
                    break
        except Exception as e:
            logger.error(f"Could not fetch command invocations: {e}")
            return {'error': f'Command not found: {command_id}', 'report_id': None}
        
        if not instance_ids:
            return {'error': 'No instances found for command', 'report_id': None}
        
        # Get command status with instance IDs
        command_status = get_command_status(command_id, instance_ids)
        
        # Get instance tags (only if we need team/product or SLA calculation)
        team = 'unknown'
        product = 'unknown'
        
        try:
            ec2_response = ec2_client.describe_instances(InstanceIds=instance_ids[:1])
            if ec2_response['Reservations']:
                tags = {tag['Key']: tag['Value'] for tag in ec2_response['Reservations'][0]['Instances'][0].get('Tags', [])}
                team = tags.get('Team', 'unknown')
                product = tags.get('Product', 'unknown')
                
                # Calculate SLA if not provided
                if not (sla_hours and sla_source) and severity:
                    frameworks_str = tags.get('ComplianceFrameworks', '')
                    if frameworks_str:
                        frameworks = [f.strip() for f in frameworks_str.split(',') if f.strip()]
                        sla_req = _calculate_sla_requirement(frameworks, severity)
                        if sla_req:
                            sla_hours = sla_req['sla_hours']
                            sla_source = sla_req['source']
        except Exception as e:
            logger.warning(f"Could not fetch instance tags: {e}")
        
        # Get timestamps from SSM command
        try:
            ssm_response = ssm_client.list_command_invocations(CommandId=command_id, MaxResults=1)
            if ssm_response['CommandInvocations']:
                invocation = ssm_response['CommandInvocations'][0]
                discovered_at = invocation['RequestedDateTime']
                patched_at = invocation.get('ResponseFinishedDateTime', datetime.now(timezone.utc))
                # Ensure both are timezone-aware for subtraction
                if discovered_at.tzinfo is None:
                    discovered_at = discovered_at.replace(tzinfo=timezone.utc)
                if patched_at.tzinfo is None:
                    patched_at = patched_at.replace(tzinfo=timezone.utc)
                time_to_patch_hours = (patched_at - discovered_at).total_seconds() / 3600
            else:
                discovered_at = datetime.now(timezone.utc)
                patched_at = datetime.now(timezone.utc)
                time_to_patch_hours = 0
        except Exception as e:
            logger.warning(f"Could not get command timestamps: {e}")
            discovered_at = datetime.now(timezone.utc)
            patched_at = datetime.now(timezone.utc)
            time_to_patch_hours = 0
        
        # Build minimal report
        timestamp = datetime.now(timezone.utc)
        report_id = f"{cve_id}-{environment}-{timestamp.strftime('%Y%m%d%H%M%S')}"
        
        report = {
            "report_id": report_id,
            "timestamp": timestamp.isoformat() + 'Z',
            "operator": get_operator(),
            
            "vulnerability": {
                "cve_id": cve_id,
                "severity": severity or "UNKNOWN",
                "cvss_score": cvss_score
            },
            
            "scope": {
                "environment": environment,
                "instance_count": len(instance_ids),
                "team": team,
                "product": product
            },
            
            "compliance": {
                "sla_hours": sla_hours,
                "sla_source": sla_source,
                "time_to_patch_hours": round(time_to_patch_hours, 2),
                "sla_met": time_to_patch_hours <= sla_hours if sla_hours else None
            },
            
            "execution": {
                "decision": decision,
                "patched_at": patched_at.isoformat() + 'Z' if isinstance(patched_at, datetime) else str(patched_at),
                "command_id": command_id,
                "status": command_status.get('status', 'UNKNOWN'),
                "success_count": command_status.get('summary', {}).get('success', 0),
                "failure_count": command_status.get('summary', {}).get('failed', 0)
            }
        }
        
        # Add compliance delta if pre/post patch states provided
        pre_patch_state = _unwrap_patch_state(pre_patch_state)
        post_patch_state = _unwrap_patch_state(post_patch_state)
        if pre_patch_state and post_patch_state:
            delta_instances = []
            total_before_missing = 0
            total_after_missing = 0
            for iid in instance_ids:
                before = pre_patch_state.get(iid, {})
                after = post_patch_state.get(iid, {})
                before_missing = before.get('missing_count', 0)
                after_missing = after.get('missing_count', 0)
                before_installed = before.get('installed_count', 0)
                after_installed = after.get('installed_count', 0)
                total_before_missing += before_missing
                total_after_missing += after_missing
                
                before_total = before_installed + before_missing
                after_total = after_installed + after_missing
                before_pct = round((before_installed / before_total * 100) if before_total > 0 else 0, 1)
                after_pct = round((after_installed / after_total * 100) if after_total > 0 else 0, 1)
                
                delta_instances.append({
                    'instance_id': iid,
                    'before_missing': before_missing,
                    'after_missing': after_missing,
                    'patches_applied': max(0, before_missing - after_missing),
                    'compliance_before_pct': before_pct,
                    'compliance_after_pct': after_pct
                })
            
            total_before = sum(d['compliance_before_pct'] for d in delta_instances)
            total_after = sum(d['compliance_after_pct'] for d in delta_instances)
            avg_before = round(total_before / len(delta_instances), 1) if delta_instances else 0
            avg_after = round(total_after / len(delta_instances), 1) if delta_instances else 0
            
            report['delta'] = {
                'instances': delta_instances,
                'total_patches_applied': total_before_missing - total_after_missing,
                'avg_compliance_before': avg_before,
                'avg_compliance_after': avg_after,
                'compliance_improvement': round(avg_after - avg_before, 1)
            }
        
        # Save to S3
        s3_client = boto3.client('s3', region_name=AWS_REGION)
        bucket_name = _get_compliance_bucket_name()
        
        # Use date-based folder structure
        date_prefix = _get_date_prefix(timestamp)
        object_key = f"{date_prefix}{report_id}.json"
        
        s3_client.put_object(
            Bucket=bucket_name,
            Key=object_key,
            Body=json.dumps(report, indent=2),
            ContentType='application/json',
            Metadata={
                'cve-id': cve_id,
                'environment': environment,
                'severity': severity or 'UNKNOWN',
                'decision-type': decision,
                'sla-met': str(report['compliance']['sla_met']) if report['compliance']['sla_met'] is not None else 'UNKNOWN',
                'team': team,
                'product': product,
                'operator': get_operator()
            }
        )
        
        result = {
            "report_id": report_id,
            "s3_location": f"s3://{bucket_name}/{object_key}",
            "sla_met": report['compliance']['sla_met'],
            "summary": {
                "cve_id": cve_id,
                "environment": environment,
                "status": report['execution']['status'],
                "instances_patched": report['execution']['success_count']
            }
        }
        
        # Include delta summary in response if available
        if 'delta' in report:
            result['delta'] = {
                'compliance_before': f"{report['delta']['avg_compliance_before']}%",
                'compliance_after': f"{report['delta']['avg_compliance_after']}%",
                'improvement': f"+{report['delta']['compliance_improvement']}%",
                'total_patches_applied': report['delta']['total_patches_applied']
            }
        
        return result
        
    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"Error generating compliance report: {error_info}")
        error_info["report_id"] = None
        return error_info
    except Exception as e:
        logger.error(f"Error generating compliance report: {e}")
        result = classify_error(e)
        result["report_id"] = None
        return result


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
        s3_client = boto3.client('s3', region_name=AWS_REGION, config=aws_config)
        bucket_name = _get_compliance_bucket_name()
        
        # Check if bucket exists
        try:
            s3_client.head_bucket(Bucket=bucket_name)
        except Exception:
            return {
                "reports": [],
                "total_count": 0,
                "message": "No compliance reports found. Generate reports first."
            }
        
        # Fetch reports
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=days_back)
        reports = _fetch_reports_from_s3(s3_client, bucket_name, start_date, end_date, 
                                         severity, environment, sla_breaches_only)
        
        # Sort by date (most recent first)
        reports.sort(key=lambda x: x['generated_at'], reverse=True)
        
        # Calculate statistics
        stats = _calculate_report_statistics(reports)
        
        # Apply limit
        limited_reports = reports[:limit]
        
        return {
            'reports': limited_reports,
            'total_count': len(reports),
            'showing': len(limited_reports),
            'date_range': f"{start_date.date()} to {end_date.date()}",
            'statistics': stats,
            'filters_applied': {
                'severity': severity,
                'environment': environment,
                'sla_breaches_only': sla_breaches_only,
                'limit': limit
            }
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
            report = future.result()
            if report and _matches_filters(report, severity, environment, sla_breaches_only):
                reports.append(report)
    
    return reports


def _parse_report_metadata(s3_client, bucket_name: str, obj: dict) -> Optional[dict]:
    """Parse S3 object metadata into report dict (KISS helper)."""
    try:
        metadata_response = s3_client.head_object(Bucket=bucket_name, Key=obj['Key'])
        metadata = metadata_response.get('Metadata', {})
        
        # Parse sla-met: 'True' → True, 'False' → False, 'UNKNOWN'/missing → None
        sla_met_raw = metadata.get('sla-met', 'UNKNOWN')
        if sla_met_raw == 'True':
            sla_met = True
        elif sla_met_raw == 'False':
            sla_met = False
        else:
            sla_met = None  # Unknown — SLA was not calculated
        
        return {
            'report_id': obj['Key'].split('/')[-1].replace('.json', ''),
            'cve_id': metadata.get('cve-id', 'unknown'),
            'severity': metadata.get('severity', 'unknown'),
            'environment': metadata.get('environment', 'unknown'),
            'decision': metadata.get('decision-type', 'unknown'),
            'sla_met': sla_met,
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
