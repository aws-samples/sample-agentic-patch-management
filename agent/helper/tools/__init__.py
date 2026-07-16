"""Tool modules for the patch automation agent.

Split by domain for:
- Independent testing and iteration
- Clearer ownership and review

Import all tools:
  from helper.tools import get_fleet_overview, execute_patch_operation, ...
"""

from .vulnerability_tools import get_vulnerability_findings, assess_fleet_impact
from .patch_tools import (
    get_patch_compliance, patch_dry_run, execute_patch_operation,
    get_command_status, rollback_patches, verify_rollback,
    multi_account_dry_run, multi_account_execute, multi_account_rollback,
    emergency_stop, get_automation_status,
)
from .compliance_tools import capture_patch_state, verify_cve_remediation, query_compliance_reports
from .fleet_tools import get_fleet_overview, resolve_execution_scope
from .maintenance_tools import (
    get_maintenance_windows, create_maintenance_window, get_patch_policy,
    check_instance_health, check_cloudwatch_alarms, verify_and_proceed,
)
from ._shared import (
    set_operator, get_operator, set_timezone, get_timezone,
    clear_request_scans, classify_error,
)

__all__ = [
    # Vulnerability
    'get_vulnerability_findings', 'assess_fleet_impact',
    # Patch operations
    'get_patch_compliance', 'patch_dry_run', 'execute_patch_operation',
    'get_command_status', 'rollback_patches', 'verify_rollback',
    'multi_account_dry_run', 'multi_account_execute', 'multi_account_rollback',
    'emergency_stop', 'get_automation_status',
    # Compliance
    'capture_patch_state', 'verify_cve_remediation', 'query_compliance_reports',
    # Fleet
    'get_fleet_overview', 'resolve_execution_scope',
    # Maintenance
    'get_maintenance_windows', 'create_maintenance_window', 'get_patch_policy',
    'check_instance_health', 'check_cloudwatch_alarms', 'verify_and_proceed',
    # Shared utilities (re-exported for backwards compat)
    'set_operator', 'get_operator', 'set_timezone', 'get_timezone',
    'clear_request_scans', 'classify_error',
]
