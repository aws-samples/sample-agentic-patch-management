"""Specific tools for patch automation - replaces generic use_aws"""

import boto3
import json
import logging
import time
import os
from strands import tool
from typing import Optional, List, Dict
from datetime import datetime, timedelta
from croniter import croniter
from profiling import profile, ENABLE_PROFILING

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize AWS clients
inspector_client = boto3.client('inspector2', region_name='us-east-1')
ssm_client = boto3.client('ssm', region_name='us-east-1')
ec2_client = boto3.client('ec2', region_name='us-east-1')
config_client = boto3.client('config', region_name='us-east-1')
cloudwatch_client = boto3.client('cloudwatch', region_name='us-east-1')


def _get_compliance_bucket_name() -> str:
    """Get S3 bucket name for compliance reports (DRY helper)."""
    sts_client = boto3.client('sts', region_name='us-east-1')
    account_id = sts_client.get_caller_identity()['Account']
    return f'patch-compliance-reports-{account_id}'


def _get_date_prefix(date: datetime) -> str:
    """Get S3 prefix for a given date (DRY helper)."""
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
        config_path = os.path.join(os.path.dirname(__file__), 'config', 'sla_policy.json')
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


@tool
@profile("Tool: get_vulnerability_findings")
def get_vulnerability_findings(cve_id: Optional[str] = None, 
                               severity: Optional[str] = None, 
                               limit: int = 50) -> dict:
    """Get vulnerability findings from AWS Inspector (ACTIVE findings only)."""
    try:
        filters = {
            'findingStatus': [{'comparison': 'EQUALS', 'value': 'ACTIVE'}]
        }
        if cve_id:
            filters['cveId'] = [{'comparison': 'EQUALS', 'value': cve_id}]
        if severity:
            filters['severity'] = [{'comparison': 'EQUALS', 'value': severity}]
        
        response = inspector_client.list_findings(
            filterCriteria=filters,
            maxResults=limit
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


@tool
def get_instance_inventory(instance_ids: Optional[List[str]] = None,
                          package_name: Optional[str] = None,
                          environment: Optional[str] = None,
                          severity: Optional[str] = None,
                          limit: Optional[int] = None) -> dict:
    """Get software inventory from AWS Systems Manager. If severity provided, includes SLA requirement."""
    try:
        # Get instances by environment tag if specified
        if environment and not instance_ids:
            ec2_response = ec2_client.describe_instances(
                Filters=[
                    {'Name': 'tag:Environment', 'Values': [environment]},
                    {'Name': 'instance-state-name', 'Values': ['running']}
                ]
            )
            instance_ids = []
            for reservation in ec2_response['Reservations']:
                for instance in reservation['Instances']:
                    instance_ids.append(instance['InstanceId'])
        
        # If no filters, get all running instances
        if not instance_ids:
            ec2_response = ec2_client.describe_instances(
                Filters=[{'Name': 'instance-state-name', 'Values': ['running']}]
            )
            instance_ids = []
            for reservation in ec2_response['Reservations']:
                for instance in reservation['Instances']:
                    instance_ids.append(instance['InstanceId'])
        
        # Apply limit if specified
        if limit and len(instance_ids) > limit:
            instance_ids = instance_ids[:limit]
        
        if not instance_ids:
            return {"instances": [], "total_count": 0}
        
        # Get inventory for instances
        instances = []
        all_frameworks = set()
        
        for instance_id in instance_ids:
            try:
                # Get SSM instance information
                ssm_info = ssm_client.describe_instance_information(
                    Filters=[{
                        'Key': 'InstanceIds',
                        'Values': [instance_id]
                    }]
                )
                
                if not ssm_info.get('InstanceInformationList'):
                    continue
                
                # Get instance tags
                ec2_response = ec2_client.describe_instances(InstanceIds=[instance_id])
                tags = ec2_response['Reservations'][0]['Instances'][0].get('Tags', [])
                env_tag = next((t['Value'] for t in tags if t['Key'] == 'Environment'), 'unknown')
                
                # Extract compliance frameworks
                frameworks_str = next((t['Value'] for t in tags if t['Key'] == 'ComplianceFrameworks'), '')
                frameworks = [f.strip() for f in frameworks_str.split(',') if f.strip()] if frameworks_str else []
                all_frameworks.update(frameworks)
                
                # Get inventory if package_name specified
                packages = []
                if package_name:
                    try:
                        inv_response = ssm_client.list_inventory_entries(
                            InstanceId=instance_id,
                            TypeName='AWS:Application'
                        )
                        for entry in inv_response.get('Entries', []):
                            if package_name.lower() in entry.get('Name', '').lower():
                                packages.append({
                                    'name': entry.get('Name'),
                                    'version': entry.get('Version')
                                })
                    except Exception:
                        pass
                
                instances.append({
                    'instance_id': instance_id,
                    'environment': env_tag,
                    'compliance_frameworks': frameworks,
                    'packages': packages
                })
            except Exception as e:
                logger.warning(f"Error getting inventory for {instance_id}: {e}")
                continue
        
        result = {"instances": instances, "total_count": len(instances)}
        
        # Calculate SLA requirement if severity provided
        if severity and instances and all_frameworks:
            sla_req = _calculate_sla_requirement(list(all_frameworks), severity)
            if sla_req:
                result['sla_requirement'] = sla_req
        
        return result
    except Exception as e:
        logger.error(f"Error getting instance inventory: {e}")
        return {"error": str(e), "instances": [], "total_count": 0}


@tool
def execute_patch_operation(instance_ids: List[str], 
                           operation: str = "Install",
                           cve_ids: Optional[List[str]] = None) -> dict:
    """Execute patch operation via AWS Systems Manager."""
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
    """Get status of SSM command execution."""
    try:
        results = []
        for instance_id in instance_ids:
            try:
                response = ssm_client.get_command_invocation(
                    CommandId=command_id,
                    InstanceId=instance_id
                )
                results.append({
                    'instance_id': instance_id,
                    'status': response['Status'],
                    'output': response.get('StandardOutputContent', '')[:500]
                })
            except Exception as e:
                results.append({
                    'instance_id': instance_id,
                    'status': 'Unknown',
                    'output': str(e)
                })
        
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


@tool
def get_maintenance_windows(environment: Optional[str] = None) -> dict:
    """Get maintenance windows from AWS Systems Manager."""
    try:
        response = ssm_client.describe_maintenance_windows()
        windows = response.get('WindowIdentities', [])
        
        if environment:
            windows = [w for w in windows if environment.lower() in w.get('Name', '').lower()]
        
        formatted_windows = []
        for window in windows:
            formatted_windows.append({
                'window_id': window.get('WindowId'),
                'name': window.get('Name'),
                'enabled': window.get('Enabled'),
                'schedule': window.get('Schedule'),
                'next_execution': window.get('NextExecutionTime')
            })
        
        return {"windows": formatted_windows, "total_count": len(formatted_windows)}
    except Exception as e:
        logger.error(f"Error getting maintenance windows: {e}")
        return {"error": str(e), "windows": [], "total_count": 0}


@tool
def create_maintenance_window(name: str, schedule: str, duration: int,
                              target_environment: str) -> dict:
    """Create maintenance window in AWS Systems Manager."""
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


@tool
def check_instance_health(instance_ids: List[str]) -> dict:
    """Check instance health after patching operations."""
    try:
        health_results = []
        
        for instance_id in instance_ids:
            try:
                # Check SSM connectivity
                ssm_info = ssm_client.describe_instance_information(
                    Filters=[{'Key': 'InstanceIds', 'Values': [instance_id]}]
                )
                
                if not ssm_info.get('InstanceInformationList'):
                    health_results.append({
                        'instance_id': instance_id,
                        'ssm_status': 'UNREACHABLE',
                        'health_score': 0,
                        'issues': ['SSM agent not responding']
                    })
                    continue
                
                instance_info = ssm_info['InstanceInformationList'][0]
                ping_status = instance_info.get('PingStatus', 'Unknown')
                
                # Health scoring
                health_score = 100 if ping_status == 'Online' else 0
                issues = [] if ping_status == 'Online' else [f'SSM ping status: {ping_status}']
                
                health_results.append({
                    'instance_id': instance_id,
                    'ssm_status': ping_status,
                    'health_score': health_score,
                    'issues': issues,
                    'last_ping': str(instance_info.get('LastPingDateTime', 'N/A'))
                })
                
            except Exception as e:
                health_results.append({
                    'instance_id': instance_id,
                    'ssm_status': 'ERROR',
                    'health_score': 0,
                    'issues': [str(e)]
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
def check_service_status(instance_ids: List[str], service_name: str = "httpd") -> dict:
    """Check if a service is running on instances."""
    try:
        command = f"systemctl is-active {service_name}"
        
        response = ssm_client.send_command(
            InstanceIds=instance_ids,
            DocumentName='AWS-RunShellScript',
            Parameters={'commands': [command]},
            TimeoutSeconds=30
        )
        
        command_id = response['Command']['CommandId']
        
        # Wait briefly for command to complete
        import time
        time.sleep(3)
        
        # Get results
        service_results = []
        for instance_id in instance_ids:
            try:
                output = ssm_client.get_command_invocation(
                    CommandId=command_id,
                    InstanceId=instance_id
                )
                
                status = output.get('Status')
                stdout = output.get('StandardOutputContent', '').strip()
                
                # systemctl is-active returns "active" if running
                is_running = (status == 'Success' and stdout == 'active')
                
                service_results.append({
                    'instance_id': instance_id,
                    'service': service_name,
                    'status': 'running' if is_running else 'stopped',
                    'is_healthy': is_running,
                    'details': stdout if not is_running else 'active'
                })
                
            except Exception as e:
                service_results.append({
                    'instance_id': instance_id,
                    'service': service_name,
                    'status': 'unknown',
                    'is_healthy': False,
                    'details': str(e)
                })
        
        running_count = sum(1 for r in service_results if r['is_healthy'])
        stopped_count = len(instance_ids) - running_count
        
        return {
            'service': service_name,
            'results': service_results,
            'running_count': running_count,
            'stopped_count': stopped_count,
            'total_instances': len(instance_ids),
            'all_healthy': stopped_count == 0,
            'recommendation': 'HEALTHY' if stopped_count == 0 else 'ROLLBACK_RECOMMENDED'
        }
        
    except Exception as e:
        logger.error(f"Error checking service status: {e}")
        return {"error": str(e)}


@tool
@profile("Tool: check_cloudwatch_alarms")
def check_cloudwatch_alarms(instance_ids: List[str], alarm_name_pattern: Optional[str] = None) -> dict:
    """
    Check CloudWatch alarms for instances.
    Returns alarms in ALARM state, grouped by instance.
    
    Args:
        instance_ids: Instance IDs to check
        alarm_name_pattern: Optional filter for alarm names (e.g., 'httpd', 'nginx')
    
    Returns:
        Health status with all alarms firing per instance
    """
    try:
        # Query all alarms in ALARM state (single API call)
        response = cloudwatch_client.describe_alarms(
            StateValue='ALARM',
            MaxRecords=100
        )
        
        alarms_in_alarm = response.get('MetricAlarms', [])
        
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
def rollback_patches(instance_ids: List[str]) -> dict:
    """Rollback patches using yum history undo."""
    try:
        # Use yum history to rollback the last transaction
        rollback_script = """
#!/bin/bash
# Get the last yum transaction ID (skip header and separator lines)
LAST_TRANSACTION=$(yum history list | grep -E '^[[:space:]]*[0-9]+' | head -1 | awk '{print $1}')

if [ -z "$LAST_TRANSACTION" ]; then
    echo "ERROR: No yum history found"
    exit 1
fi

echo "Rolling back transaction ID: $LAST_TRANSACTION"
yum history undo $LAST_TRANSACTION -y

if [ $? -eq 0 ]; then
    echo "SUCCESS: Rollback completed for transaction $LAST_TRANSACTION"
    exit 0
else
    echo "ERROR: Rollback failed for transaction $LAST_TRANSACTION"
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
            'message': 'Rollback command sent. Use get_command_status to verify completion.'
        }
        
    except Exception as e:
        logger.error(f"Error initiating rollback: {e}")
        return {"error": str(e), "instance_ids": instance_ids}


@tool
def get_resource_dependencies(resource_id: str) -> dict:
    """
    Analyze resource dependencies using AWS Config as primary source.
    
    Returns comprehensive view including:
    - All relationships tracked by Config
    - Load balancer health status
    - Business context from tags
    - Blast radius assessment
    - Rollback recommendation
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
            elbv2_client = boto3.client('elbv2', region_name='us-east-1')
            
            for tg_rel in target_group_relationships:
                try:
                    # Get target group details
                    tg_arn = tg_rel['resourceId']
                    tg_response = elbv2_client.describe_target_groups(TargetGroupArns=[tg_arn])
                    
                    if tg_response['TargetGroups']:
                        tg = tg_response['TargetGroups'][0]
                        
                        # Get health status
                        health = elbv2_client.describe_target_health(TargetGroupArn=tg_arn)
                        healthy = sum(1 for t in health['TargetHealthDescriptions'] 
                                     if t['TargetHealth']['State'] == 'healthy')
                        total = len(health['TargetHealthDescriptions'])
                        
                        # Get ALB name
                        alb_name = 'unknown'
                        if tg.get('LoadBalancerArns'):
                            albs = elbv2_client.describe_load_balancers(
                                LoadBalancerArns=tg['LoadBalancerArns']
                            )
                            if albs['LoadBalancers']:
                                alb_name = albs['LoadBalancers'][0]['LoadBalancerName']
                        
                        load_balancers.append({
                            'name': alb_name,
                            'target_group': tg['TargetGroupName'],
                            'healthy_targets': healthy,
                            'total_targets': total,
                            'status': 'HEALTHY' if healthy == total else 'DEGRADED' if healthy > 0 else 'UNHEALTHY'
                        })
                except Exception as e:
                    logger.warning(f"Could not query target group {tg_rel['resourceId']}: {e}")
        
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
                'infrastructure': infrastructure,
                'total_relationships': len(relationships)
            },
            'recommendation': recommendation,
            'summary': f"{blast_radius} blast radius - {impact_description}",
            'source': 'AWS Config',
            'config_capture_time': config_item.get('configurationItemCaptureTime')
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


@tool
def get_maintenance_windows(environment: str = None, instance_ids: list = None) -> dict:
    """Get maintenance window information and validate if specific instances are covered."""
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
                        # Remove 'cron(' prefix and ')' suffix
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
    """Generate compliance report with SLA tracking from policy file and organizational metadata from tags."""
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
        s3_client = boto3.client('s3', region_name='us-east-1')
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
    """Query compliance reports from S3 for the last N days with summary statistics."""
    try:
        s3_client = boto3.client('s3', region_name='us-east-1')
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
    """Fetch and filter reports from S3 (KISS helper)."""
    reports = []
    days_diff = (end_date - start_date).days + 1
    
    for day_offset in range(days_diff):
        check_date = start_date + timedelta(days=day_offset)
        prefix = _get_date_prefix(check_date)
        
        try:
            response = s3_client.list_objects_v2(Bucket=bucket_name, Prefix=prefix)
            for obj in response.get('Contents', []):
                report = _parse_report_metadata(s3_client, bucket_name, obj)
                if report and _matches_filters(report, severity, environment, sla_breaches_only):
                    reports.append(report)
        except Exception as e:
            logger.debug(f"No reports for {prefix}: {e}")
            continue
    
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
