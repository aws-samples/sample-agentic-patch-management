"""
Common SSM queries and templates for the SSM Agent
"""

COMMON_QUERIES = {
    "critical_patches": "Show instances missing critical security patches",
    "patch_scan_results": "Get latest patch scan results for all instances",
    "failed_installations": "List recent failed patch installations with error details",
    "reboot_pending": "Show instances requiring reboot after patching",
    "patch_baseline_compliance": "Check compliance against specific patch baseline",
    "maintenance_window_status": "Get current status of all maintenance windows",
    "patch_group_assignments": "Show which instances belong to which patch groups",
    "automation_execution_logs": "Get detailed logs from patch automation executions"
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
