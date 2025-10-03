"""Simple, Flexible Patch Execution Tools - Strands Best Practice"""

from strands import tool
from strands_tools import use_aws
from typing import List, Dict, Any

# NOTE: These tools are built but not yet integrated into agents
# They follow Strands best practice: simple tools, agent-driven logic

@tool
def send_patch_command(instance_ids: List[str], operation: str = "Install") -> Dict[str, Any]:
    """Send patch command to instances via Systems Manager."""
    return use_aws(
        service_name="ssm",
        operation_name="send_command",
        parameters={
            "DocumentName": "AWS-RunPatchBaseline",
            "InstanceIds": instance_ids,
            "Parameters": {"Operation": [operation]}
        },
        region="us-east-1",
        label=f"Send patch command to {len(instance_ids)} instances"
    )

@tool  
def create_maintenance_window(name: str, schedule: str, duration: int) -> Dict[str, Any]:
    """Create maintenance window for scheduling."""
    return use_aws(
        service_name="ssm",
        operation_name="create_maintenance_window",
        parameters={
            "Name": name,
            "Schedule": schedule, 
            "Duration": duration
        },
        region="us-east-1",
        label=f"Create maintenance window: {name}"
    )

@tool
def get_command_status(command_id: str) -> Dict[str, Any]:
    """Get status of Systems Manager command."""
    return use_aws(
        service_name="ssm",
        operation_name="list_command_invocations",
        parameters={"CommandId": command_id},
        region="us-east-1",
        label=f"Get command status: {command_id}"
    )
