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
}

export interface DashboardData {
  environments: EnvironmentStatus[];
  findings: VulnerabilityFinding[];
  severityCounts: Record<string, number>;
  activities: ActivityItem[];
  compliance: ComplianceSummary | null;
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
 * Single fetch — server runs all 4 data sources concurrently via asyncio.gather.
 * One HTTP request, no event-loop blocking, no queuing.
 */
export async function fetchDashboard(): Promise<DashboardData> {
  const res = await fetch("/api/dashboard");
  if (!res.ok) throw new Error(`Dashboard API: ${res.status}`);

  const raw = await res.json();

  return {
    environments: enrichEnvironments(raw.environments ?? [], raw.findings ?? []),
    findings: raw.findings ?? [],
    severityCounts: raw.severity_counts ?? {},
    activities: raw.activities ?? [],
    compliance: raw.compliance ?? null,
    errors: raw.errors ?? [],
  };
}
