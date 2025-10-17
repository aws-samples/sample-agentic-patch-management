"""Generic AWS API tool as fallback"""

import boto3
import logging
from strands import tool
from typing import Dict, Any

logger = logging.getLogger(__name__)

@tool
def use_aws(
    service_name: str,
    operation_name: str,
    parameters: Dict[str, Any],
    region: str = "us-east-1",
    label: str = "AWS API Call"
) -> dict:
    """
    Call any AWS API operation using boto3.
    
    Args:
        service_name: AWS service name (e.g., 'ssm', 'inspector2', 'ec2')
        operation_name: API operation name in snake_case (e.g., 'send_command', 'list_findings')
        parameters: Dictionary of parameters for the operation
        region: AWS region (default: 'us-east-1')
        label: Human-readable description of the operation
    
    Returns:
        API response as dictionary
    
    Examples:
        # Check service status via SSM
        use_aws(
            service_name="ssm",
            operation_name="send_command",
            parameters={
                "InstanceIds": ["i-xxx"],
                "DocumentName": "AWS-RunShellScript",
                "Parameters": {"commands": ["systemctl is-active httpd"]}
            },
            region="us-east-1",
            label="Check httpd service status"
        )
        
        # Get ACTIVE vulnerability findings (critical severity)
        use_aws(
            service_name="inspector2",
            operation_name="list_findings",
            parameters={
                "filterCriteria": {
                    "findingStatus": [{"comparison": "EQUALS", "value": "ACTIVE"}],
                    "severity": [{"comparison": "EQUALS", "value": "CRITICAL"}]
                },
                "maxResults": 50
            },
            region="us-east-1",
            label="List critical vulnerabilities"
        )
    """
    try:
        # Create client for the specified service
        client = boto3.client(service_name, region_name=region)
        
        # Validate operation exists
        if not hasattr(client, operation_name):
            available_ops = [op for op in dir(client) if not op.startswith('_')]
            return {
                "error": f"Operation '{operation_name}' not found in service '{service_name}'",
                "available_operations": available_ops[:20]  # Show first 20
            }
        
        operation_method = getattr(client, operation_name)
        
        # Call the operation with parameters
        response = operation_method(**parameters)
        
        # Remove ResponseMetadata for cleaner output
        if 'ResponseMetadata' in response:
            del response['ResponseMetadata']
        
        return response
        
    except Exception as e:
        logger.error(f"Error calling {service_name}.{operation_name}: {e}")
        return {
            "error": str(e),
            "service_name": service_name,
            "operation_name": operation_name
        }
