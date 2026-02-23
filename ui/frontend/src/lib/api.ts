/** Typed API client for the single /api/dashboard endpoint. */

export interface EnvironmentStatus {
  environment: string;
  total: number;
  online: number;
  offline: number;
  status: "healthy" | "warning" | "error" | "inactive";
  vulns: Record<string, number>;
  vuln_total: number;
}

export interface VulnerabilityFinding {
  cve_id: string;
  severity: string;
  cvss_score: number | null;
  title: string;
  environment: string;
  resource_id: string;
  fix_available: string;
}

export interface ActivityItem {
  report_key: string;
  timestamp: string;
  cve_id: string;
  environment: string;
  severity: string;
  decision: string;
  sla_met: string;
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
  time_to_patch_hours: number | null;
  sla_met: boolean | null;
  status: string;
  success_count: number;
  failure_count: number;
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
  errors: string[];
}

/** Compute per-environment vuln counts from findings (avoids extra API calls). */
function enrichEnvironments(
  envs: Array<Omit<EnvironmentStatus, "vulns" | "vuln_total">>,
  findings: VulnerabilityFinding[]
): EnvironmentStatus[] {
  const envVulns: Record<string, Record<string, number>> = {};
  for (const f of findings) {
    if (!envVulns[f.environment]) envVulns[f.environment] = {};
    envVulns[f.environment][f.severity] = (envVulns[f.environment][f.severity] ?? 0) + 1;
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
    errors: raw.errors ?? [],
  };
}
