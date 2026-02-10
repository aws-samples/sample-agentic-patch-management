/** Typed fetch wrappers for dashboard API endpoints. */

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

/** All dashboard data in one bundle for coordinated refresh. */
export interface DashboardData {
  environments: EnvironmentStatus[];
  findings: VulnerabilityFinding[];
  severityCounts: Record<string, number>;
  activities: ActivityItem[];
  compliance: ComplianceSummary | null;
  errors: string[];
}

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`API ${url}: ${res.status}`);
  return res.json();
}

/** Fetch all dashboard data in parallel. Partial failures are captured, not thrown. */
export async function fetchDashboard(): Promise<DashboardData> {
  const errors: string[] = [];

  const [envRes, vulnRes, actRes, compRes] = await Promise.allSettled([
    fetchJson<{ environments: EnvironmentStatus[] }>("/api/dashboard/environments"),
    fetchJson<{ findings: VulnerabilityFinding[]; severity_counts: Record<string, number> }>("/api/dashboard/vulnerabilities"),
    fetchJson<{ activities: ActivityItem[] }>("/api/dashboard/activity"),
    fetchJson<{ summary: ComplianceSummary }>("/api/dashboard/compliance"),
  ]);

  if (envRes.status === "rejected") errors.push(`Environments: ${envRes.reason}`);
  if (vulnRes.status === "rejected") errors.push(`Vulnerabilities: ${vulnRes.reason}`);
  if (actRes.status === "rejected") errors.push(`Activity: ${actRes.reason}`);
  if (compRes.status === "rejected") errors.push(`Compliance: ${compRes.reason}`);

  return {
    environments: envRes.status === "fulfilled" ? envRes.value.environments : [],
    findings: vulnRes.status === "fulfilled" ? vulnRes.value.findings : [],
    severityCounts: vulnRes.status === "fulfilled" ? vulnRes.value.severity_counts : {},
    activities: actRes.status === "fulfilled" ? actRes.value.activities : [],
    compliance: compRes.status === "fulfilled" ? compRes.value.summary : null,
    errors,
  };
}
