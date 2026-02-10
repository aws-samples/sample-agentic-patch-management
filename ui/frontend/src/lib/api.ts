/** Typed fetch wrappers for dashboard API endpoints. */

export interface EnvironmentStatus {
  environment: string;
  total: number;
  online: number;
  offline: number;
  status: "healthy" | "warning" | "error" | "inactive";
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

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`API ${url}: ${res.status}`);
  return res.json();
}

export const api = {
  environments: () =>
    fetchJson<{ environments: EnvironmentStatus[] }>("/api/dashboard/environments"),
  vulnerabilities: () =>
    fetchJson<{ findings: VulnerabilityFinding[]; total: number; severity_counts: Record<string, number> }>(
      "/api/dashboard/vulnerabilities"
    ),
  activity: () =>
    fetchJson<{ activities: ActivityItem[]; total: number }>("/api/dashboard/activity"),
  compliance: () =>
    fetchJson<{ summary: ComplianceSummary }>("/api/dashboard/compliance"),
};
