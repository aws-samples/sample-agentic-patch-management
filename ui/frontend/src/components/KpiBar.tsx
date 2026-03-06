import { PieChart, Pie, Cell, ResponsiveContainer } from "recharts";
import { Card, CardContent } from "./ui/Card";
import type { DashboardData } from "../lib/api";

interface Props {
  data: DashboardData;
}

const COLORS = {
  compliant: "#10b981",
  nonCompliant: "#ef4444",
  dev: "#3b82f6",
  staging: "#f59e0b",
  prod: "#ef4444",
  critical: "#dc2626",
  high: "#f97316",
  medium: "#eab308",
  low: "#6b7280",
  empty: "#e5e7eb",
};

export default function KpiBar({ data }: Props) {
  const totalInstances = data.environments.reduce((s, e) => s + e.total, 0);
  const criticalVulns = data.severityCounts["CRITICAL"] ?? 0;
  const highVulns = data.severityCounts["HIGH"] ?? 0;
  const mediumVulns = data.severityCounts["MEDIUM"] ?? 0;
  const lowVulns = data.severityCounts["LOW"] ?? 0;
  const totalVulns = Object.values(data.severityCounts).reduce((s, n) => s + n, 0);
  const totalReports = data.compliance?.total_reports ?? 0;
  const slaBreached = data.compliance?.sla_breached ?? 0;

  // Instance-level compliance from live SSM patch state (consistent with Patch Manager console)
  const envCompliance = data.environments.reduce(
    (acc, e) => {
      if (e.patch_compliance) {
        acc.compliant += e.patch_compliance.compliant_instances;
        acc.scanned += e.patch_compliance.scanned_instances;
        acc.missing += e.patch_compliance.missing_patches;
      }
      return acc;
    },
    { compliant: 0, scanned: 0, missing: 0 }
  );
  const instanceComplianceRate = envCompliance.scanned > 0
    ? Math.round((envCompliance.compliant / envCompliance.scanned) * 100 * 10) / 10
    : 0;
  const hasComplianceData = envCompliance.scanned > 0;

  const scope = data.scopes?.[0];
  const scopeDetail = scope
    ? `${scope.accountId} · ${scope.region}`
    : "unknown";

  // Donut data
  const complianceData = hasComplianceData
    ? [{ value: envCompliance.compliant, color: COLORS.compliant }, { value: envCompliance.scanned - envCompliance.compliant, color: COLORS.nonCompliant }]
    : [{ value: 1, color: COLORS.empty }];

  const criticalData = criticalVulns > 0
    ? [{ value: criticalVulns, color: COLORS.critical }, { value: Math.max(totalInstances - criticalVulns, 0), color: COLORS.empty }]
    : [{ value: 1, color: COLORS.compliant }];

  const envVulnData = data.environments.map(e => ({ value: e.vuln_total || 1, color: e.environment === "prod" ? COLORS.prod : e.environment === "staging" ? COLORS.staging : COLORS.dev }));
  if (envVulnData.length === 0) envVulnData.push({ value: 1, color: COLORS.empty });

  return (
    <div className="space-y-3">
      {/* Scope line */}
      <div className="text-[11px] text-gray-400">
        🔒 Account: {scopeDetail}
      </div>

      {/* Donut charts row */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <DonutCard
          title="Patch Compliance"
          centerValue={hasComplianceData ? `${instanceComplianceRate}%` : "—"}
          data={complianceData}
          legend={hasComplianceData ? [
            { label: `Compliant ${envCompliance.compliant}`, color: COLORS.compliant },
            { label: `Non-Compliant ${envCompliance.scanned - envCompliance.compliant}`, color: COLORS.nonCompliant },
          ] : [{ label: "No scan data", color: COLORS.empty }]}
        />
        <DonutCard
          title="Critical Patches Missing"
          centerValue={criticalVulns.toString()}
          data={criticalData}
          legend={data.environments.filter(e => (e.vulns["CRITICAL"] ?? 0) > 0).map(e => ({
            label: `${e.environment.charAt(0).toUpperCase() + e.environment.slice(1)} ${e.vulns["CRITICAL"]}`,
            color: e.environment === "prod" ? COLORS.prod : e.environment === "staging" ? COLORS.staging : COLORS.dev,
          }))}
        />
        <DonutCard
          title="Total Vulnerabilities"
          centerValue={totalVulns.toLocaleString()}
          data={[
            { value: criticalVulns || 0, color: COLORS.critical },
            { value: highVulns || 0, color: COLORS.high },
            { value: mediumVulns || 0, color: COLORS.medium },
            { value: lowVulns || 0, color: COLORS.low },
          ].filter(d => d.value > 0).length > 0
            ? [
              { value: criticalVulns || 0, color: COLORS.critical },
              { value: highVulns || 0, color: COLORS.high },
              { value: mediumVulns || 0, color: COLORS.medium },
              { value: lowVulns || 0, color: COLORS.low },
            ].filter(d => d.value > 0)
            : [{ value: 1, color: COLORS.empty }]
          }
          legend={[
            ...(criticalVulns > 0 ? [{ label: `Critical ${criticalVulns}`, color: COLORS.critical }] : []),
            ...(highVulns > 0 ? [{ label: `High ${highVulns}`, color: COLORS.high }] : []),
            ...(mediumVulns > 0 ? [{ label: `Medium ${mediumVulns}`, color: COLORS.medium }] : []),
            ...(lowVulns > 0 ? [{ label: `Low ${lowVulns}`, color: COLORS.low }] : []),
          ]}
        />
      </div>

      {/* Bottom row: Environment Summary + SLA Breaches */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <EnvironmentSummary environments={data.environments} />
        <SlaBreachesCard breached={slaBreached} totalReports={totalReports} />
      </div>
    </div>
  );
}


/* ── Donut Chart Card ─────────────────────────────────────────── */

function DonutCard({ title, centerValue, data, legend }: {
  title: string;
  centerValue: string;
  data: { value: number; color: string }[];
  legend: { label: string; color: string }[];
}) {
  return (
    <Card className="hover:shadow-md transition-shadow">
      <CardContent className="p-4">
        <h3 className="text-sm font-semibold text-gray-700 mb-2">{title}</h3>
        <div className="relative flex items-center justify-center" style={{ height: 160 }}>
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie
                data={data}
                cx="50%"
                cy="50%"
                innerRadius={50}
                outerRadius={70}
                dataKey="value"
                stroke="none"
                startAngle={90}
                endAngle={-270}
              >
                {data.map((entry, i) => (
                  <Cell key={i} fill={entry.color} />
                ))}
              </Pie>
            </PieChart>
          </ResponsiveContainer>
          {/* Center label */}
          <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
            <span className="text-3xl font-bold text-gray-900">{centerValue}</span>
          </div>
        </div>
        {/* Legend */}
        <div className="flex flex-wrap gap-3 justify-center mt-1">
          {legend.map((item, i) => (
            <span key={i} className="flex items-center gap-1 text-[11px] text-gray-600">
              <span className="w-2.5 h-2.5 rounded-full inline-block" style={{ backgroundColor: item.color }} />
              {item.label}
            </span>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

/* ── Environment Summary ──────────────────────────────────────── */

function EnvironmentSummary({ environments }: { environments: DashboardData["environments"] }) {
  const totalVulns = environments.reduce((s, e) => s + e.vuln_total, 0);
  const maxVulns = Math.max(...environments.map(e => e.vuln_total), 1);

  return (
    <Card className="hover:shadow-md transition-shadow">
      <CardContent className="p-4">
        <h3 className="text-sm font-semibold text-gray-700 mb-3">Vulnerabilities by Environment</h3>

        {/* Vulnerability bars (stacked by severity) */}
        <p className="text-[11px] text-gray-500 mb-1.5">Unique CVEs: {totalVulns}</p>
        <div className="space-y-1.5 mb-2">
          {environments.map(env => {
            const c = env.vulns["CRITICAL"] ?? 0;
            const h = env.vulns["HIGH"] ?? 0;
            const m = env.vulns["MEDIUM"] ?? 0;
            const l = env.vulns["LOW"] ?? 0;
            const total = c + h + m + l;
            return (
              <div key={env.environment + "-v"} className="flex items-center gap-2">
                <span className="text-[11px] text-gray-600 w-14 text-right capitalize">{env.environment}</span>
                <div className="flex-1 bg-gray-100 rounded-full h-4 overflow-hidden flex">
                  {c > 0 && <div className="h-full" style={{ width: `${(c / maxVulns) * 100}%`, backgroundColor: COLORS.critical }} />}
                  {h > 0 && <div className="h-full" style={{ width: `${(h / maxVulns) * 100}%`, backgroundColor: COLORS.high }} />}
                  {m > 0 && <div className="h-full" style={{ width: `${(m / maxVulns) * 100}%`, backgroundColor: COLORS.medium }} />}
                  {l > 0 && <div className="h-full" style={{ width: `${(l / maxVulns) * 100}%`, backgroundColor: COLORS.low }} />}
                  <span className="text-[9px] text-gray-600 ml-1.5 self-center">{total}</span>
                </div>
              </div>
            );
          })}
        </div>
        <div className="flex gap-3 text-[10px] text-gray-500">
          <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full" style={{ backgroundColor: COLORS.critical }} />Critical</span>
          <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full" style={{ backgroundColor: COLORS.high }} />High</span>
          <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full" style={{ backgroundColor: COLORS.medium }} />Medium</span>
          <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full" style={{ backgroundColor: COLORS.low }} />Low</span>
        </div>
      </CardContent>
    </Card>
  );
}

/* ── SLA Breaches Card ────────────────────────────────────────── */

function SlaBreachesCard({ breached, totalReports }: { breached: number; totalReports: number }) {
  return (
    <Card className={`hover:shadow-md transition-shadow ${breached > 0 ? "border-l-4 border-l-red-500" : ""}`}>
      <CardContent className="p-4 flex flex-col items-center justify-center h-full">
        <h3 className="text-sm font-semibold text-gray-700 mb-2">SLA Breaches (30 days)</h3>
        <div className={`text-6xl font-bold ${breached > 0 ? "text-red-600" : "text-emerald-600"}`}>
          {breached}
        </div>
        <p className={`text-sm mt-2 ${breached > 0 ? "text-red-500" : "text-emerald-500"}`}>
          {breached > 0 ? "Requires attention" : totalReports > 0 ? "All SLAs met" : "No reports yet"}
        </p>
      </CardContent>
    </Card>
  );
}
