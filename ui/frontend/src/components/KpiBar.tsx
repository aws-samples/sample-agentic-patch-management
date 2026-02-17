import { Card, CardContent } from "./ui/Card";
import type { DashboardData } from "../lib/api";

interface Props {
  data: DashboardData;
}

export default function KpiBar({ data }: Props) {
  const totalInstances = data.environments.reduce((s, e) => s + e.total, 0);
  const onlineInstances = data.environments.reduce((s, e) => s + e.online, 0);
  const offlineInstances = totalInstances - onlineInstances;
  const criticalVulns = data.severityCounts["CRITICAL"] ?? 0;
  const highVulns = data.severityCounts["HIGH"] ?? 0;
  const mediumVulns = data.severityCounts["MEDIUM"] ?? 0;
  const totalVulns = Object.values(data.severityCounts).reduce((s, n) => s + n, 0);
  const slaRate = data.compliance?.sla_rate_percent ?? 0;
  const totalReports = data.compliance?.total_reports ?? 0;
  const slaBreached = data.compliance?.sla_breached ?? 0;
  const envCount = data.environments.length;

  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
      <KpiCard label="Environments" value={envCount.toString()} detail={`${totalInstances} instances · ${offlineInstances > 0 ? `${offlineInstances} offline` : "all online"}`} icon="🌐" />
      <KpiCard label="Instances Online" value={`${onlineInstances}/${totalInstances}`} detail={onlineInstances === totalInstances ? "all healthy" : `${offlineInstances} need attention`} color={onlineInstances === totalInstances ? "green" : "red"} icon="🖥️" />
      <KpiCard label="Critical Vulns" value={criticalVulns.toString()} detail={criticalVulns === 0 ? "none detected" : "immediate action needed"} color={criticalVulns > 0 ? "red" : "green"} icon="🚨" />
      <KpiCard label="High Vulns" value={highVulns.toString()} detail={`${mediumVulns} medium · ${totalVulns} total`} color={highVulns > 0 ? "amber" : undefined} icon="⚠️" />
      <KpiCard label="SLA Compliance" value={totalReports > 0 ? `${slaRate}%` : "—"} detail={totalReports > 0 ? `${totalReports} reports · 30d` : "no reports yet"} color={totalReports > 0 ? (slaRate >= 95 ? "green" : slaRate >= 80 ? "amber" : "red") : undefined} icon="📊" />
      <KpiCard label="SLA Breaches" value={slaBreached.toString()} detail={slaBreached === 0 ? "none in 30 days" : "review required"} color={slaBreached > 0 ? "red" : "green"} icon="🔔" />
    </div>
  );
}

const ACCENT = {
  green: "border-l-emerald-500",
  red: "border-l-red-500",
  amber: "border-l-amber-500",
} as const;

const BG_TINT = {
  green: "",
  red: "bg-red-50/40",
  amber: "bg-amber-50/30",
} as const;

const VALUE_COLOR = {
  green: "text-emerald-600",
  red: "text-red-700",
  amber: "text-amber-700",
} as const;

function KpiCard({ label, value, detail, color, icon }: { label: string; value: string; detail: string; color?: "green" | "red" | "amber"; icon: string }) {
  return (
    <Card className={`border-l-4 ${color ? ACCENT[color] : "border-l-gray-300"} ${color ? BG_TINT[color] : ""} hover:shadow-md hover:-translate-y-0.5 transition-all duration-200 cursor-default`}>
      <CardContent className="p-3.5">
        <div className="flex items-center gap-1.5 mb-1">
          <span className="text-sm">{icon}</span>
          <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider">{label}</span>
        </div>
        <div className={`text-2xl font-bold ${color ? VALUE_COLOR[color] : "text-gray-900"}`}>
          {value}
        </div>
        <div className="text-[11px] text-gray-500 mt-0.5">{detail}</div>
      </CardContent>
    </Card>
  );
}
