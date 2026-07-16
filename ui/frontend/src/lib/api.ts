/** Typed API client for the single /api/dashboard endpoint. */
// Build: 2026-03-06

export interface PerAccountEnv {
  total: number;
  online: number;
  offline: number;
  patch_compliance: {
    compliant_instances: number;
    scanned_instances: number;
    compliance_pct: number | null;
    missing_patches: number;
    installed_patches: number;
    failed_patches: number;
  };
}

export interface EnvironmentStatus {
  environment: string;
  total: number;
  online: number;
  offline: number;
  status: "healthy" | "warning" | "error" | "inactive";
  vulns: Record<string, number>;
  vuln_total: number;
  accounts?: string[];
  per_account?: Record<string, PerAccountEnv>;
  patch_compliance?: {
    compliant_instances: number;
    scanned_instances: number;
    compliance_pct: number | null;
    missing_patches: number;
    installed_patches: number;
    failed_patches: number;
  };
}

export interface VulnerabilityFinding {
  cve_id: string;
  severity: string;
  cvss_score: number | null;
  title: string;
  environment: string;
  environments: string[];
  accounts: string[];
  /**
   * Regions where this CVE was observed (Inspector is regional, so a CVE
   * may show up across multiple regions). May be empty for older payloads.
   */
  regions?: string[];
  services: string[];
  resource_id: string;
  fix_available: string;
  instance_count: number;
}

export interface ActivityItem {
  report_key: string;
  timestamp: string;
  operation_type: "patch" | "rollback";
  cve_id: string | null;
  environment: string;
  severity: string | null;
  decision: string | null;
  sla_met: string | null;
  instance_count?: number;
  status?: string;
}

export interface ComplianceSummary {
  total_reports: number;
  sla_met: number;
  sla_breached: number;
  sla_rate_percent: number;
  period_days: number;
  by_severity: Record<string, { total: number; met: number; breached: number }>;
  by_environment: Record<string, { total: number; met: number; breached: number }>;
  by_team: Record<string, { total: number; met: number; breached: number }>;
}

export interface ReportDetail {
  report_id: string;
  timestamp: string;
  operator: string;
  cve_id: string;
  severity: string;
  cvss_score: number | null;
  environment: string;
  team: string;
  product: string;
  instance_count: number;
  decision: string;
  sla_hours: number | null;
  sla_source: string;
  frameworks: string[];
  sla_met: boolean | null;
  status: string;
  success_count: number;
  failure_count: number;
}

export interface RunningOperation {
  execution_id: string;
  operation_type: "patch" | "rollback";
  environment: string;
  started_at: string | null;
  targeting: string | null;
  instance_count: number;
  status: string;
}

export interface AccountScope {
  accountId: string;
  region: string;
}

export interface DashboardData {
  scopes: AccountScope[];
  environments: EnvironmentStatus[];
  findings: VulnerabilityFinding[];
  severityCounts: Record<string, number>;
  activities: ActivityItem[];
  compliance: ComplianceSummary | null;
  reportDetails: ReportDetail[];
  runningOperations: RunningOperation[];
  errors: string[];
  warnings: Array<{ type: string; title: string; message: string }>;
}

/** Compute per-environment vuln counts from findings (avoids extra API calls).
 *
 * Each CVE entry has `environments[]` — the union of envs where that CVE was
 * observed (a single CVE can hit prod + dev + staging on different instances).
 * Counting against the legacy singular `f.environment` would only attribute the
 * CVE to the FIRST env seen, hiding it from the other env cards.
 */
function enrichEnvironments(
  envs: Array<Omit<EnvironmentStatus, "vulns" | "vuln_total">>,
  findings: VulnerabilityFinding[]
): EnvironmentStatus[] {
  const envVulns: Record<string, Record<string, number>> = {};
  for (const f of findings) {
    const envList = (f.environments && f.environments.length > 0)
      ? f.environments
      : [f.environment];
    for (const env of envList) {
      if (!envVulns[env]) envVulns[env] = {};
      envVulns[env][f.severity] = (envVulns[env][f.severity] ?? 0) + 1;
    }
  }
  return envs.map((e) => {
    const vulns = envVulns[e.environment] ?? {};
    return { ...e, vulns, vuln_total: Object.values(vulns).reduce((s, n) => s + n, 0) };
  });
}

/**
 * Single fetch — server runs all 3 data sources concurrently via asyncio.gather.
 * One HTTP request, no event-loop blocking, no queuing.
 * Pass force=true to bypass server-side cache (manual refresh).
 */
export async function fetchDashboard(force = false): Promise<DashboardData> {
  const url = force ? "/api/dashboard?force=true" : "/api/dashboard";
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Dashboard API: ${res.status}`);

  const raw = await res.json();

  return {
    scopes: (raw.scopes ?? []).map((s: { account_id: string; region: string }) => ({
      accountId: s.account_id ?? "unknown",
      region: s.region ?? "unknown",
    })),
    environments: enrichEnvironments(raw.environments ?? [], raw.findings ?? []),
    findings: raw.findings ?? [],
    severityCounts: raw.severity_counts ?? {},
    activities: raw.activities ?? [],
    compliance: raw.compliance ?? null,
    reportDetails: raw.report_details ?? [],
    runningOperations: raw.running_operations ?? [],
    errors: raw.errors ?? [],
    warnings: raw.warnings ?? [],
  };
}
// build 1772754702
