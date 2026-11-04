/** Typed API client for the single /api/dashboard endpoint. */
import { apiFetch } from "./http";

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

export interface ComplianceSummary {
  total_reports: number;
  sla_met: number;
  sla_breached: number;
  sla_unknown?: number;
  /** met / (met + breached); null when no run has a known SLA result. */
  sla_rate_percent: number | null;
  period_days: number;
  by_severity: Record<string, { total: number; met: number; breached: number }>;
  by_environment: Record<string, { total: number; met: number; breached: number }>;
  /** SLA results grouped by the first REPORT_TAGS tag. */
  by_tag?: { key: string | null; groups: Record<string, { total: number; met: number; breached: number }> };
}

/** One patch or rollback run from the compliance reports (last 30 days). */
export interface RunRow {
  report_id: string;
  timestamp: string;
  operation_type: "patch" | "rollback";
  execution_id: string | null;
  plan_id: string | null;
  operator: string;
  environment: string;
  /** REPORT_TAGS values recorded when the run started ("mixed" when the instances differ). */
  tags: Record<string, string>;
  tag_values?: Record<string, string[]>;
  instance_count: number;
  cve_id: string | null;
  severity: string | null;
  decision: string | null;
  /** Why a run went ahead now (agent RUN_NOW_REASONS); absent on reports from before it was recorded. */
  decision_reason?: string | null;
  status: string;
  success_count: number;
  failure_count: number;
  failure_message: string;
  duration_seconds: number | null;
  sla_met: boolean | null;
  sla_hours: number | null;
  sla_source: string | null;
  sla_reason: string | null;
  /** When the finding was first seen; the SLA deadline is this plus sla_hours. Missing on older servers. */
  first_observed_at?: string | null;
  /** The severity a severity run patched (many findings, no single discovery time). */
  severity_filter?: string | null;
  frameworks: string[];
  install_packages: string[];
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
  /** Per severity: distinct finding IDs, affected instances, IDs with a fix. */
  findingsSummary: Record<string, { ids: number; instances: number; fixable_ids: number }>;
  /** Accounts and Regions whose instances couldn't be read, so aren't counted. */
  unreadableLocations: Array<{ account_id: string; region: string; instances: number; error: string }>;
  compliance: ComplianceSummary | null;
  runs: RunRow[];
  runningOperations: RunningOperation[];
  errors: string[];
  /** A read that failed has a `*_unreadable` type and tone "risk"; Explorer errors carry the error code. */
  warnings: Array<{ type: string; title: string; message: string; tone?: "risk"; code?: string }>;
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
  const res = await apiFetch(url);
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
    findingsSummary: raw.findings_summary ?? {},
    unreadableLocations: raw.unreadable_locations ?? [],
    compliance: raw.compliance ?? null,
    runs: raw.runs ?? [],
    runningOperations: raw.running_operations ?? [],
    errors: raw.errors ?? [],
    warnings: raw.warnings ?? [],
  };
}
