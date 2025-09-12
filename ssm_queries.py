"""
Common SSM queries and templates for the SSM Agent
"""

COMMON_QUERIES = {
    "patch_baselines": "List all available patch baselines in AWS Systems Manager",
    "patch_groups": "Show all patch groups configured in Systems Manager",
    "maintenance_windows": "List all maintenance windows and their schedules",
    "compliance_summary": "Get patch compliance summary for all instances",
    "non_compliant_instances": "Show instances that are not patch compliant",
    "recent_patch_operations": "List recent patch operations and their status",
    "ssm_documents": "List all SSM documents available for patch management",
    "parameter_store": "Show parameters in Systems Manager Parameter Store",
    "automation_executions": "List recent automation executions",
    "inventory_summary": "Get inventory summary for managed instances"
}

def get_query_suggestions():
    """Return formatted query suggestions for user reference."""
    suggestions = []
    for key, description in COMMON_QUERIES.items():
        suggestions.append(f"  {key}: {description}")
    return "\n".join(suggestions)

def get_query_by_key(key: str) -> str:
    """Get a predefined query by its key."""
    return COMMON_QUERIES.get(key, f"Unknown query key: {key}")
