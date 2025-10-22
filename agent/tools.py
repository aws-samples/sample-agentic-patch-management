"""Specific tools for patch automation"""

import boto3
from botocore.config import Config
import json
import logging
import time
import os
from strands import tool
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from croniter import croniter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# AWS client configuration with retry logic
aws_config = Config(
    retries={
        'max_attempts': 3,
        'mode': 'adaptive'  # Adaptive retry mode for better handling of throttling
    }
)

# Initialize AWS clients with retry configuration
inspector_client = boto3.client('inspector2', region_name='us-east-1', config=aws_config)
ssm_client = boto3.client('ssm', region_name='us-east-1', config=aws_config)
ec2_client = boto3.client('ec2', region_name='us-east-1', config=aws_config)
config_client = boto3.client('config', region_name='us-east-1', config=aws_config)
cloudwatch_client = boto3.client('cloudwatch', region_name='us-east-1', config=aws_config)


# ============================================================================
# PRIVATE HELPER FUNCTIONS
# ============================================================================

def _get_compliance_bucket_name() -> str:
    """Get S3 bucket name for compliance reports."""
    sts_client = boto3.client('sts', region_name='us-east-1', config=aws_config)
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
        config_path = os.path.join(os.path.dirname(__file__), 'config', 'config.json')
        with open(config_path, 'r') as f:
            sla_policy = json.load(f)
        
        sla_hours = None
        source_framework = None
        
        # Check each framework for SLA, use strictest (minimum)
        if frameworks:
            for framework in frameworks:
                if framework in sla_policy.get('frameworks', {}):
                    framework_sla = sla_policy['frameworks'][framework].get(severity.upper())
                    if framework_sla and (sla_hours is None or framework_sla < sla_hours):
                        sla_hours = framework_sla
                        source_framework = framework
        
        # Fall back to default if no framework SLA found
        if sla_hours is None:
            sla_hours = sla_policy.get('default', {}).get(severity.upper(), 24)
            source_framework = "default"
        
        return {
            "sla_hours": sla_hours,
            "frameworks": frameworks,
            "source": source_framework,
            "severity": severity.upper()
        }
    except Exception as e:
        logger.warning(f"Could not calculate SLA: {e}")
        return None

def _normalize_environment(user_input: str) -> str:
    """Map user input to canonical environment tag value."""
    config_path = os.path.join(os.path.dirname(__file__), 'config', 'config.json')
    with open(config_path, 'r') as f:
        config = json.load(f)
    
    aliases = config.get('environment_aliases', {})
    user_lower = user_input.lower()
    
    for canonical, alias_list in aliases.items():
        if user_lower in alias_list:
            return canonical
    
    return user_input


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
        filters = {
            'findingStatus': [{'comparison': 'EQUALS', 'value': 'ACTIVE'}]
        }
        if cve_id:
            filters['cveId'] = [{'comparison': 'EQUALS', 'value': cve_id}]
        if severity:
            filters['severity'] = [{'comparison': 'EQUALS', 'value': severity}]
        if environment:
            env_value = _normalize_environment(environment)
            filters['resourceTags'] = [{'comparison': 'EQUALS', 'key': 'Environment', 'value': env_value}]
        
        # Sort by severity (most critical first)
        response = inspector_client.list_findings(
            filterCriteria=filters,
            sortCriteria={
                'field': 'SEVERITY',
                'sortOrder': 'DESC'
            },
            maxResults=min(limit, 100)  # Inspector max is 100
        )
        
        # list_findings returns full finding objects, not just ARNs
        findings_data = response.get('findings', [])
        if not findings_data:
            return {"findings": [], "total_count": 0}
        
        findings = []
        for finding in findings_data:
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
        
        return {
            "findings": findings,
            "total_count": len(findings)
        }
    except Exception as e:
        logger.error(f"Error getting vulnerability findings: {e}")
        return {"error": str(e), "findings": [], "total_count": 0}


# --- Patch Management ---

@tool
def get_patch_compliance(environment: Optional[str] = None, 
                        instance_ids: Optional[List[str]] = None,
                        severity: Optional[str] = None) -> dict:
    """Get patch compliance status from AWS Systems Manager.
    
    Args:
        environment: Filter by environment (dev, staging, prod)
        instance_ids: Specific instance IDs to check
        severity: If provided, includes SLA calculation based on compliance frameworks
    
    Returns:
        dict: {'instances': list, 'total_count': int, 'summary': dict, 'sla_requirement': dict (optional)}
    """
    try:
        instance_tags = {}
        
        # Get instance IDs by environment if specified
        if environment and not instance_ids:
            env_value = _normalize_environment(environment)
            ec2_response = ec2_client.describe_instances(
                Filters=[
                    {'Name': 'tag:Environment', 'Values': [env_value]},
                    {'Name': 'instance-state-name', 'Values': ['running']}
                ]
            )
            instance_ids = []
            for reservation in ec2_response['Reservations']:
                for instance in reservation['Instances']:
                    instance_id = instance['InstanceId']
                    instance_ids.append(instance_id)
                    # Extract tags for SLA calculation
                    if severity:
                        tags = {tag['Key']: tag['Value'] for tag in instance.get('Tags', [])}
                        instance_tags[instance_id] = tags
        
        if not instance_ids:
            return {"instances": [], "total_count": 0}
        
        # Single API call to get patch states for all instances
        response = ssm_client.describe_instance_patch_states(InstanceIds=instance_ids)
        
        instances = []
        for state in response.get('InstancePatchStates', []):
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
        
        result = {
            "instances": instances,
            "total_count": len(instances),
            "summary": {
                "total_missing": sum(i['missing_count'] for i in instances),
                "total_security_non_compliant": sum(i['security_non_compliant_count'] for i in instances),
                "instances_needing_patches": sum(1 for i in instances if i['missing_count'] > 0)
            }
        }
        
        # Calculate SLA if severity provided
        if severity and instance_tags:
            # Get compliance frameworks from first instance
            first_instance_tags = next(iter(instance_tags.values()), {})
            frameworks_str = first_instance_tags.get('ComplianceFrameworks', '')
            frameworks = [f.strip() for f in frameworks_str.split(',') if f.strip()] if frameworks_str else []
            
            if frameworks:
                sla_req = _calculate_sla_requirement(frameworks, severity)
                if sla_req:
                    result['sla_requirement'] = sla_req
        
        return result
    except Exception as e:
        logger.error(f"Error getting patch compliance: {e}")
        return {"error": str(e), "instances": [], "total_count": 0}


@tool
def execute_patch_operation(instance_ids: List[str], 
                           operation: str = "Install",
                           cve_ids: Optional[List[str]] = None) -> dict:
    """Execute patch operation via AWS Systems Manager.
    
    Args:
        instance_ids: List of EC2 instance IDs to patch
        operation: Patch operation type (default: 'Install')
        cve_ids: Optional list of specific CVE IDs (requires custom patch baseline)
    
    Returns:
        dict: {'command_id': str, 'status': str, 'instance_count': int}
    """
    try:
        params = {
            'Operation': [operation],
            'RebootOption': ['NoReboot']
        }
        
        if cve_ids:
            logger.warning("CVE-specific filtering requires custom patch baseline")
        
        response = ssm_client.send_command(
            DocumentName='AWS-RunPatchBaseline',
            InstanceIds=instance_ids,
            Parameters=params
        )
        
        return {
            'command_id': response['Command']['CommandId'],
            'status': response['Command']['Status'],
            'instance_count': len(instance_ids)
        }
    except Exception as e:
        logger.error(f"Error executing patch operation: {e}")
        return {"error": str(e)}


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
    except Exception as e:
        logger.error(f"Error getting command status: {e}")
        return {"error": str(e)}


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
    except Exception as e:
        logger.error(f"Error creating maintenance window: {e}")
        return {"error": str(e)}


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
        
    except Exception as e:
        logger.error(f"Error checking instance health: {e}")
        return {"error": str(e)}


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
        
    except Exception as e:
        logger.error(f"Error checking CloudWatch alarms: {e}", exc_info=True)
        return {
            "error": str(e),
            "instance_ids": instance_ids,
            "recommendation": "UNKNOWN"
        }


@tool
def verify_and_proceed(command_id: str, instance_ids: List[str], 
                       environment: str, alarm_pattern: Optional[str] = "httpd") -> dict:
    """Verify patch completion and health before proceeding to next environment.
    
    This is an OPTIONAL tool for production-ready verification. Use when:
    - User explicitly requests verification ("verify and proceed")
    - Patching production environment (higher safety requirements)
    - User wants to wait for completion before moving forward
    
    For demos/dev environments, users can say "proceed" without verification.
    
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
        
    except Exception as e:
        logger.error(f"Error in verify_and_proceed: {e}")
        return {
            'status': 'ERROR',
            'recommendation': 'INVESTIGATE',
            'reason': str(e)
        }


@tool
def rollback_patches(instance_ids: List[str]) -> dict:
    """Rollback patches on EC2 instances using yum history.
    
    Args:
        instance_ids: List of EC2 instance IDs to rollback patches on
    
    Returns:
        dict: {'command_id': str, 'status': str, 'instance_count': int}
    """
    try:
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
        
    except Exception as e:
        logger.error(f"Error initiating rollback: {e}")
        return {
            "error": str(e), 
            "instance_ids": instance_ids,
            "suggestion": "Rollback command could not be sent. Verify SSM connectivity and instance state."
        }


@tool
def get_resource_dependencies(resource_id: str) -> dict:
    """Analyze resource dependencies using AWS Config.
    
    Args:
        resource_id: EC2 instance ID to analyze dependencies for
    
    Returns:
        dict: {
            'resource_id': str,
            'relationships': list,
            'load_balancers': list,
            'blast_radius': str,
            'recommendation': str
        }
    """
    try:
        # Query AWS Config for instance configuration
        config_response = config_client.get_resource_config_history(
            resourceId=resource_id,
            resourceType='AWS::EC2::Instance',
            limit=1
        )
        
        if not config_response.get('configurationItems'):
            return {"error": f"Instance {resource_id} not found in AWS Config"}
        
        config_item = config_response['configurationItems'][0]
        configuration = json.loads(config_item['configuration'])
        relationships = config_item.get('relationships', [])
        
        # Extract tags from Config
        tags = {tag['key']: tag['value'] for tag in configuration.get('tags', [])}
        environment = tags.get('Environment', 'unknown')
        criticality = tags.get('Criticality', 'Unknown')
        team = tags.get('Team', 'unknown')
        product = tags.get('Product', 'unknown')
        compliance = tags.get('ComplianceFrameworks', '')
        
        # Find load balancer relationships from Config
        load_balancers = []
        target_group_relationships = [r for r in relationships 
                                     if r['resourceType'] == 'AWS::ElasticLoadBalancingV2::TargetGroup']
        
        if target_group_relationships:
            elbv2_client = boto3.client('elbv2', region_name='us-east-1', config=aws_config)
            
            # Batch: Get all target group ARNs at once
            tg_arns = [r['resourceId'] for r in target_group_relationships]
            
            try:
                # Single API call for all target groups
                tg_response = elbv2_client.describe_target_groups(TargetGroupArns=tg_arns)
                tg_map = {tg['TargetGroupArn']: tg for tg in tg_response['TargetGroups']}
                
                # Collect all ALB ARNs
                alb_arns = []
                for tg in tg_response['TargetGroups']:
                    alb_arns.extend(tg.get('LoadBalancerArns', []))
                
                # Single API call for all ALBs
                alb_map = {}
                if alb_arns:
                    albs_response = elbv2_client.describe_load_balancers(LoadBalancerArns=list(set(alb_arns)))
                    alb_map = {alb['LoadBalancerArn']: alb['LoadBalancerName'] for alb in albs_response['LoadBalancers']}
                
                # Get health for each target group (can't batch this)
                for tg_arn in tg_arns:
                    try:
                        tg = tg_map.get(tg_arn)
                        if not tg:
                            continue
                        
                        # Get health status
                        health = elbv2_client.describe_target_health(TargetGroupArn=tg_arn)
                        healthy = sum(1 for t in health['TargetHealthDescriptions'] 
                                     if t['TargetHealth']['State'] == 'healthy')
                        total = len(health['TargetHealthDescriptions'])
                        
                        # Get ALB name from map
                        alb_name = 'unknown'
                        if tg.get('LoadBalancerArns'):
                            alb_arn = tg['LoadBalancerArns'][0]
                            alb_name = alb_map.get(alb_arn, 'unknown')
                        
                        load_balancers.append({
                            'name': alb_name,
                            'target_group': tg['TargetGroupName'],
                            'healthy_targets': healthy,
                            'total_targets': total,
                            'status': 'HEALTHY' if healthy == total else 'DEGRADED' if healthy > 0 else 'UNHEALTHY'
                        })
                    except Exception as e:
                        logger.warning(f"Could not get health for target group {tg_arn}: {e}")
                        
            except Exception as e:
                logger.warning(f"Could not query target groups: {e}")
        
        # Calculate blast radius
        blast_radius = 'LOW'
        impact_description = 'Infrastructure dependencies only'
        
        if load_balancers:
            if criticality == 'High' or environment == 'prod':
                blast_radius = 'HIGH'
                impact_description = 'Customer-facing services affected'
            elif criticality == 'Medium' or environment == 'staging':
                blast_radius = 'MEDIUM'
                impact_description = 'Internal services affected'
        
        # Generate recommendation
        recommendation = 'MONITOR'
        if load_balancers:
            degraded = [lb for lb in load_balancers if lb['status'] != 'HEALTHY']
            if degraded:
                recommendation = 'ROLLBACK RECOMMENDED - Load balancer health degraded'
        
        # Extract infrastructure relationships
        infrastructure = {
            'vpc': next((r['resourceId'] for r in relationships if r['resourceType'] == 'AWS::EC2::VPC'), None),
            'subnet': next((r['resourceId'] for r in relationships if r['resourceType'] == 'AWS::EC2::Subnet'), None),
            'security_groups': [r['resourceId'] for r in relationships if r['resourceType'] == 'AWS::EC2::SecurityGroup']
        }
        
        return {
            'resource_id': resource_id,
            'environment': environment,
            'blast_radius': blast_radius,
            'impact_description': impact_description,
            'business_context': {
                'criticality': criticality,
                'team': team,
                'product': product,
                'compliance_frameworks': compliance.split(',') if compliance else []
            },
            'dependencies': {
                'load_balancers': load_balancers,
                'infrastructure': infrastructure
            },
            'recommendation': recommendation,
            'summary': f"{blast_radius} blast radius - {impact_description}"
        }
        
    except Exception as e:
        logger.error(f"Error analyzing dependencies for {resource_id}: {e}")
        return {
            'error': str(e),
            'resource_id': resource_id
        }
        
    except Exception as e:
        logger.error(f"Error getting resource dependencies: {e}")
        return {"error": str(e), "resource_id": resource_id}


# --- Compliance & Reporting ---

@tool
def get_maintenance_windows(environment: str = None, instance_ids: list = None) -> dict:
    """Get maintenance window information and validate instance coverage.
    
    Args:
        environment: Filter by environment (dev, staging, prod)
        instance_ids: Optional list of instance IDs to validate coverage
    
    Returns:
        dict: {'windows': list, 'instance_coverage': dict (optional)}
    """
    try:
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
                        
                        cron = croniter(cron_expr, datetime.utcnow())
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
                if environment and window_environment != environment:
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
        
    except Exception as e:
        logger.error(f"Error getting maintenance windows: {e}")
        return {
            'error': str(e),
            'windows': [],
            'count': 0
        }


@tool
def generate_compliance_report(
    cve_id: str,
    severity: str,
    decision: str,
    justification: str,
    affected_instances: list,
    execution_results: dict,
    environment: str = 'unknown',
    discovered_at: str = None,
    patched_at: str = None
) -> dict:
    """Generate compliance report with SLA tracking and save to S3.
    
    Args:
        cve_id: CVE identifier (e.g., 'CVE-2024-1234')
        severity: Vulnerability severity (CRITICAL, HIGH, MEDIUM, LOW)
        decision: Patching decision (EMERGENCY, SCHEDULED, DEFERRED)
        justification: Reason for the decision
        affected_instances: List of affected instance IDs
        execution_results: Results from patch execution
        environment: Target environment (default: 'unknown')
        discovered_at: Discovery timestamp (optional)
        patched_at: Patching timestamp (optional)
    
    Returns:
        dict: {'report_id': str, 's3_location': str, 'sla_status': str}
    """
    try:
        import json
        import os
        
        # Generate timestamp
        timestamp = datetime.utcnow()
        timestamp_str = timestamp.strftime('%H%M%SZ')
        date_str = timestamp.strftime('%Y-%m-%d')
        
        # Generate report ID
        report_id = f"{cve_id}-{date_str}-{timestamp_str}"
        
        # Get instance tags for organizational metadata and compliance frameworks
        instance_metadata = {}
        compliance_frameworks = []
        sla_requirement_hours = None
        sla_source = None
        
        if affected_instances:
            try:
                ec2_response = ec2_client.describe_instances(InstanceIds=affected_instances[:1])
                if ec2_response['Reservations']:
                    instance = ec2_response['Reservations'][0]['Instances'][0]
                    tags = {tag['Key']: tag['Value'] for tag in instance.get('Tags', [])}
                    
                    # Extract compliance frameworks
                    frameworks_str = tags.get('ComplianceFrameworks', '')
                    if frameworks_str:
                        compliance_frameworks = [f.strip() for f in frameworks_str.split(',') if f.strip()]
                    
                    # Calculate SLA requirement using helper function
                    if compliance_frameworks:
                        sla_req = _calculate_sla_requirement(compliance_frameworks, severity)
                        if sla_req:
                            sla_requirement_hours = sla_req['sla_hours']
                            sla_source = sla_req['source']
                    
                    # Extract organizational metadata
                    instance_metadata = {
                        'team': tags.get('Team', 'unknown'),
                        'product': tags.get('Product', 'unknown'),
                        'cost_center': tags.get('CostCenter', 'unknown'),
                        'owner': tags.get('Owner', 'unknown'),
                        'compliance_frameworks': compliance_frameworks,
                        'criticality': tags.get('Criticality', 'unknown')
                    }
            except Exception as tag_error:
                logger.warning(f"Could not extract instance metadata: {tag_error}")
        
        # Calculate SLA metrics if we have timestamps and SLA requirement
        sla_tracking = None
        if discovered_at and patched_at and sla_requirement_hours:
            try:
                discovered = datetime.fromisoformat(discovered_at.replace('Z', '+00:00'))
                patched = datetime.fromisoformat(patched_at.replace('Z', '+00:00'))
                time_to_patch_hours = (patched - discovered).total_seconds() / 3600
                
                sla_tracking = {
                    'severity': severity,
                    'discovered_at': discovered_at,
                    'patched_at': patched_at,
                    'time_to_patch_hours': round(time_to_patch_hours, 2),
                    'sla_requirement_hours': sla_requirement_hours,
                    'sla_source': sla_source,
                    'sla_met': time_to_patch_hours <= sla_requirement_hours,
                    'sla_breach_hours': max(0, round(time_to_patch_hours - sla_requirement_hours, 2))
                }
            except Exception as sla_error:
                logger.warning(f"Could not calculate SLA metrics: {sla_error}")
        
        # Build comprehensive report
        report = {
            'report_id': report_id,
            'report_version': '2.0',
            'generated_at': timestamp.isoformat() + 'Z',
            'cve_details': {
                'cve_id': cve_id,
                'severity': severity,
                'cvss_score': execution_results.get('cvss_score'),
                'description': execution_results.get('description')
            },
            'decision': {
                'type': decision,
                'justification': justification,
                'decided_at': timestamp.isoformat() + 'Z',
                'decision_maker': 'intelligent-patch-automation'
            },
            'affected_systems': {
                'environment': environment,
                'instance_ids': affected_instances,
                'instance_count': len(affected_instances),
                'organizational_context': instance_metadata
            },
            'sla_tracking': sla_tracking,
            'execution': {
                'status': execution_results.get('status', 'unknown'),
                'command_id': execution_results.get('command_id'),
                'started_at': execution_results.get('started_at'),
                'completed_at': execution_results.get('completed_at'),
                'success_count': execution_results.get('success_count', 0),
                'failure_count': execution_results.get('failure_count', 0),
                'rollback_performed': execution_results.get('rollback_performed', False)
            },
            'metadata': {
                'report_version': '2.0',
                'generated_by': 'intelligent-patch-automation',
                'schema_version': '2.0',
                'queryable_fields': [
                    'cve_id', 'severity', 'environment', 'decision.type',
                    'sla_tracking.sla_met', 'organizational_context.team',
                    'organizational_context.product', 'organizational_context.compliance_frameworks',
                    'execution.status', 'organizational_context.criticality'
                ]
            }
        }
        
        # Try to save to S3
        s3_client = boto3.client('s3', region_name='us-east-1', config=aws_config)
        bucket_name = _get_compliance_bucket_name()
        
        # Generate S3 key with date-based folder structure
        date_prefix = _get_date_prefix(timestamp)
        object_key = f"{date_prefix}{cve_id}-{environment}-{timestamp_str}.json"
        
        try:
            # Upload to S3
            s3_client.put_object(
                Bucket=bucket_name,
                Key=object_key,
                Body=json.dumps(report, indent=2),
                ContentType='application/json',
                Metadata={
                    'cve-id': cve_id,
                    'severity': severity,
                    'environment': environment,
                    'decision-type': decision,
                    'sla-met': str(sla_tracking['sla_met']) if sla_tracking else 'unknown',
                    'team': instance_metadata.get('team', 'unknown'),
                    'product': instance_metadata.get('product', 'unknown')
                }
            )
            
            report_url = f"s3://{bucket_name}/{object_key}"
            
            return {
                'report_id': report_id,
                'report_url': report_url,
                'storage': 's3',
                'summary': {
                    'cve_id': cve_id,
                    'severity': severity,
                    'decision': decision,
                    'environment': environment,
                    'affected_instances': len(affected_instances),
                    'status': execution_results.get('status', 'unknown'),
                    'sla_met': sla_tracking['sla_met'] if sla_tracking else None,
                    'sla_requirement_hours': sla_requirement_hours,
                    'team': instance_metadata.get('team', 'unknown'),
                    'product': instance_metadata.get('product', 'unknown'),
                    'compliance_frameworks': compliance_frameworks
                }
            }
            
        except Exception as s3_error:
            # S3 failed, return report content locally
            logger.warning(f"Could not save to S3: {s3_error}")
            
            return {
                'report_id': report_id,
                'storage': 'local',
                'storage_error': str(s3_error),
                'report_content': report,
                'summary': {
                    'cve_id': cve_id,
                    'severity': severity,
                    'decision': decision,
                    'environment': environment,
                    'affected_instances': len(affected_instances),
                    'status': execution_results.get('status', 'unknown'),
                    'sla_met': sla_tracking['sla_met'] if sla_tracking else None,
                    'sla_requirement_hours': sla_requirement_hours,
                    'team': instance_metadata.get('team', 'unknown'),
                    'product': instance_metadata.get('product', 'unknown'),
                    'compliance_frameworks': compliance_frameworks
                }
            }
            
    except Exception as e:
        logger.error(f"Error generating compliance report: {e}")
        return {
            'error': str(e),
            'report_id': None
        }


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
        s3_client = boto3.client('s3', region_name='us-east-1', config=aws_config)
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
        end_date = datetime.utcnow()
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
        
    except Exception as e:
        logger.error(f"Error querying compliance reports: {e}")
        return {"error": str(e), "reports": [], "total_count": 0}


def _fetch_reports_from_s3(s3_client, bucket_name: str, start_date: datetime, 
                           end_date: datetime, severity: Optional[str], 
                           environment: Optional[str], sla_breaches_only: bool) -> list:
    """Fetch and filter reports from S3 with optimized queries."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    reports = []
    days_diff = (end_date - start_date).days + 1
    
    # Collect all S3 objects first (with pagination)
    objects_to_check = []
    for day_offset in range(days_diff):
        check_date = start_date + timedelta(days=day_offset)
        prefix = _get_date_prefix(check_date)
        
        try:
            # Paginate through list_objects_v2
            continuation_token = None
            while True:
                params = {'Bucket': bucket_name, 'Prefix': prefix}
                if continuation_token:
                    params['ContinuationToken'] = continuation_token
                
                response = s3_client.list_objects_v2(**params)
                objects_to_check.extend(response.get('Contents', []))
                
                if not response.get('IsTruncated'):
                    break
                continuation_token = response.get('NextContinuationToken')
                
        except Exception as e:
            logger.debug(f"No reports for {prefix}: {e}")
            continue
    
    if not objects_to_check:
        return []
    
    # Parallel head_object calls (max 10 concurrent)
    def fetch_metadata(obj):
        return _parse_report_metadata(s3_client, bucket_name, obj)
    
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
        
        return {
            'report_id': obj['Key'].split('/')[-1].replace('.json', ''),
            'cve_id': metadata.get('cve-id', 'unknown'),
            'severity': metadata.get('severity', 'unknown'),
            'environment': metadata.get('environment', 'unknown'),
            'decision': metadata.get('decision-type', 'unknown'),
            'sla_met': metadata.get('sla-met') == 'True',
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
    if sla_breaches_only and report['sla_met']:
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
            'by_severity': {},
            'by_team': {}
        }
    
    sla_breaches = sum(1 for r in reports if not r['sla_met'])
    breach_rate = (sla_breaches / total * 100)
    
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
    
    return {
        'total_reports': total,
        'sla_breaches': sla_breaches,
        'sla_breach_rate': round(breach_rate, 1),
        'by_severity': severity_breakdown,
        'by_team': team_breakdown
    }


# ============================================================================
# FALLBACK TOOL
# ============================================================================

@tool
def use_aws(
    service_name: str,
    operation_name: str,
    parameters: Dict[str, Any],
    region: str = "us-east-1",
    label: str = "AWS API Call"
) -> dict:
    """Call any AWS API operation using boto3.
    
    Args:
        service_name: AWS service name (e.g., 'ssm', 'inspector2', 'ec2')
        operation_name: API operation name in snake_case (e.g., 'send_command')
        parameters: Dictionary of parameters for the operation
        region: AWS region (default: 'us-east-1')
        label: Human-readable description of the operation
    
    Returns:
        dict: API response (limited to first 15 items for list operations)
    """
    try:
        client = boto3.client(service_name, region_name=region, config=aws_config)
        
        if not hasattr(client, operation_name):
            available_ops = [op for op in dir(client) if not op.startswith('_')]
            return {
                "error": f"Operation '{operation_name}' not found in service '{service_name}'",
                "available_operations": available_ops[:20]
            }
        
        operation_method = getattr(client, operation_name)
        
        response = operation_method(**parameters)
        
        # Remove ResponseMetadata for cleaner output
        if 'ResponseMetadata' in response:
            del response['ResponseMetadata']
        
        # Limit large list responses to prevent formatting overhead
        if 'Reservations' in response and len(response['Reservations']) > 0:
            # EC2 instances - limit to 15
            total_instances = sum(len(r.get('Instances', [])) for r in response['Reservations'])
            if total_instances > 15:
                # Keep first 15 instances
                count = 0
                limited_reservations = []
                for reservation in response['Reservations']:
                    if count >= 15:
                        break
                    instances = reservation.get('Instances', [])
                    remaining = 15 - count
                    if len(instances) <= remaining:
                        limited_reservations.append(reservation)
                        count += len(instances)
                    else:
                        limited_res = reservation.copy()
                        limited_res['Instances'] = instances[:remaining]
                        limited_reservations.append(limited_res)
                        count = 15
                
                response['Reservations'] = limited_reservations
                response['_truncated'] = True
                response['_total_count'] = total_instances
                response['_showing'] = 15
        
        return response
        
    except Exception as e:
        logger.error(f"Error calling {service_name}.{operation_name}: {e}")
        return {
            "error": str(e),
            "service_name": service_name,
            "operation_name": operation_name
        }
