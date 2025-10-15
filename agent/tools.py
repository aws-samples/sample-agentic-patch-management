"""Specific tools for patch automation - replaces generic use_aws"""

import boto3
import logging
from strands import tool
from typing import Optional, List, Dict
from datetime import datetime
from croniter import croniter

logger = logging.getLogger(__name__)

# Initialize AWS clients
inspector_client = boto3.client('inspector2', region_name='us-east-1')
ssm_client = boto3.client('ssm', region_name='us-east-1')
ec2_client = boto3.client('ec2', region_name='us-east-1')
config_client = boto3.client('config', region_name='us-east-1')


@tool
def get_vulnerability_findings(cve_id: Optional[str] = None, 
                               severity: Optional[str] = None, 
                               limit: int = 50) -> dict:
    """Get vulnerability findings from AWS Inspector."""
    try:
        filters = {}
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
                          limit: Optional[int] = None) -> dict:
    """Get software inventory from AWS Systems Manager."""
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
                    'packages': packages
                })
            except Exception as e:
                logger.warning(f"Error getting inventory for {instance_id}: {e}")
                continue
        
        return {"instances": instances, "total_count": len(instances)}
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
        
        # Load SLA policy
        config_path = os.path.join(os.path.dirname(__file__), 'config', 'sla_policy.json')
        try:
            with open(config_path, 'r') as f:
                sla_policy = json.load(f)
        except Exception as policy_error:
            logger.warning(f"Could not load SLA policy: {policy_error}")
            sla_policy = None
        
        # Get instance tags for organizational metadata and compliance frameworks
        instance_metadata = {}
        compliance_frameworks = []
        sla_requirement_hours = None
        
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
                    
                    # Determine SLA requirement from compliance frameworks
                    if sla_policy and compliance_frameworks:
                        # Get SLA for each framework, use strictest (minimum)
                        framework_slas = []
                        for framework in compliance_frameworks:
                            if framework in sla_policy.get('frameworks', {}):
                                framework_sla = sla_policy['frameworks'][framework].get(severity.upper())
                                if framework_sla:
                                    framework_slas.append(framework_sla)
                        
                        if framework_slas:
                            sla_requirement_hours = min(framework_slas)
                            sla_source = f"compliance_framework ({','.join(compliance_frameworks)})"
                        else:
                            # Framework not in policy, use default
                            sla_requirement_hours = sla_policy.get('default', {}).get(severity.upper())
                            sla_source = "default_policy"
                    elif sla_policy:
                        # No frameworks, use default
                        sla_requirement_hours = sla_policy.get('default', {}).get(severity.upper())
                        sla_source = "default_policy"
                    
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
        
        # Get account ID for bucket name
        sts_client = boto3.client('sts', region_name='us-east-1')
        account_id = sts_client.get_caller_identity()['Account']
        bucket_name = f'patch-compliance-reports-{account_id}'
        
        # Generate S3 key with date-based folder structure
        object_key = f"{timestamp.year}/{timestamp.month:02d}/{timestamp.day:02d}/{cve_id}-{environment}-{timestamp_str}.json"
        
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
