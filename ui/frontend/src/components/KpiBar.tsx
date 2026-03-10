import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip } from "recharts";
import { Card, CardContent } from "./ui/Card";
import type { DashboardData } from "../lib/api";

interface Props {
  data: DashboardData;
  onTabChange?: (tab: string) => void;
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

export default function KpiBar({ data, onTabChange }: Props) {
  const totalInstances = data.environments.reduce((s, e) => s + e.total, 0);
  const criticalVulns = data.severityCounts["CRITICAL"] ?? 0;
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

  // Donut data
  const complianceData = hasComplianceData
    ? [{ value: envCompliance.compliant, color: COLORS.compliant }, { value: envCompliance.scanned - envCompliance.compliant, color: COLORS.nonCompliant }]
    : [{ value: 1, color: COLORS.empty }];

  const criticalData = criticalVulns > 0
    ? [{ value: criticalVulns, color: COLORS.critical }, { value: Math.max(totalInstances - criticalVulns, 0), color: COLORS.empty }]
    : [{ value: 1, color: COLORS.compliant }];

  return (
    <div className="space-y-3">
      {/* Donut charts row */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <DonutCard
          title="Patch Compliance"
          centerValue={hasComplianceData ? `${instanceComplianceRate}%` : "—"}
          data={complianceData}
          legend={hasComplianceData ? [
            { label: `Compliant ${envCompliance.compliant}`, color: COLORS.compliant },
            { label: `Non-Compliant ${envCompliance.scanned - envCompliance.compliant}`, color: COLORS.nonCompliant },
          ] : [{ label: "No scan data", color: COLORS.empty }]}
          onClick={() => onTabChange?.("overview")}
        />
        <DonutCard
          title="Critical Patches Missing"
          centerValue={criticalVulns.toString()}
          data={criticalData}
          legend={data.environments.filter(e => (e.vulns["CRITICAL"] ?? 0) > 0).map(e => ({
            label: `${e.environment.charAt(0).toUpperCase() + e.environment.slice(1)} ${e.vulns["CRITICAL"]}`,
            color: e.environment === "prod" ? COLORS.prod : e.environment === "staging" ? COLORS.staging : COLORS.dev,
          }))}
          onClick={() => onTabChange?.("vulnerabilities")}
        />
      </div>

      {/* Bottom row: SLA Breaches */}
      <div className="grid grid-cols-1 gap-4">
        <SlaBreachesCard breached={slaBreached} totalReports={totalReports} onClick={() => onTabChange?.("compliance")} />
      </div>
    </div>
  );
}


/* ── Donut Chart Card ─────────────────────────────────────────── */

function DonutCard({ title, centerValue, data, legend, onClick }: {
  title: string;
  centerValue: string;
  data: { value: number; color: string }[];
  legend: { label: string; color: string }[];
  onClick?: () => void;
}) {
  return (
    <Card className={`hover:shadow-md transition-shadow ${onClick ? "cursor-pointer" : ""}`} onClick={onClick}>
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
              <Tooltip formatter={(value: number) => value.toLocaleString()} />
            </PieChart>
          </ResponsiveContainer>
          {/* Center label */}
          <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
            <span className="text-3xl font-bold text-gray-900" aria-label={`${title}: ${centerValue}`}>{centerValue}</span>
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

/* ── SLA Breaches Card ────────────────────────────────────────── */

function SlaBreachesCard({ breached, totalReports, onClick }: { breached: number; totalReports: number; onClick?: () => void }) {
  return (
    <Card className={`hover:shadow-md transition-shadow ${breached > 0 ? "border-l-4 border-l-red-500" : ""} ${onClick ? "cursor-pointer" : ""}`} onClick={onClick}>
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
