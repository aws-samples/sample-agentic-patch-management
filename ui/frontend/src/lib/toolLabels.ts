/** Plain-words progress labels for agent tools, shown while they run. */
const TOOL_LABELS: Record<string, string> = {
  get_fleet_overview: "Reading the fleet",
  resolve_execution_scope: "Finding accounts in scope",
  get_vulnerability_findings: "Checking Inspector findings",
  assess_fleet_impact: "Mapping the CVE across environments",
  get_patch_compliance: "Reading patch compliance",
  patch_dry_run: "Scanning instances",
  multi_account_dry_run: "Scanning the environment",
  execute_patch_operation: "Preparing a patch plan",
  multi_account_execute: "Preparing a patch plan",
  rollback_patches: "Preparing a rollback plan",
  multi_account_rollback: "Preparing a rollback plan",
  create_maintenance_window: "Preparing a window plan",
  get_command_status: "Checking command status",
  get_automation_status: "Checking run status",
  verify_rollback: "Checking the rollback",
  verify_and_proceed: "Checking the run and health",
  get_maintenance_windows: "Checking maintenance windows",
  get_patch_policy: "Checking patch policies",
  capture_patch_state: "Recording patch counts",
  verify_cve_remediation: "Checking Inspector for the CVE",
  query_compliance_reports: "Reading compliance reports",
  forecast_sla_misses: "Forecasting SLA deadlines",
  check_instance_health: "Checking instance health",
  check_cloudwatch_alarms: "Checking alarms",
};

/** Label for a tool, or a readable fallback for one added later. */
export function toolLabel(name?: string): string {
  if (!name) return "";
  const known = TOOL_LABELS[name];
  if (known) return known;
  const words = name.replace(/^get_/, "").replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}
