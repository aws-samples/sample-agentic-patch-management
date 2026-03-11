import { useMemo, useState } from "react";
import { ChevronLeftIcon, ChevronRightIcon } from "@heroicons/react/24/outline";
import { Badge, sevVariant, decisionVariant, SlaBadge } from "./ui/Badge";
import { Card, CardContent } from "./ui/Card";
import type { ComplianceSummary as ComplianceData, ReportDetail } from "../lib/api";

/** Parse timestamps that may have a trailing Z after a UTC offset (e.g. +00:00Z) */
function parseTimestamp(ts: string): Date {
  return new Date(ts.replace(/([+-]\d{2}:\d{2})Z$/, "$1"));
}

interface Props {
  data: ComplianceData | null;
  reports: ReportDetail[];
  loading: boolean;
}

const PAGE_SIZE = 10;

export default function ComplianceSummary({ data, reports, loading }: Props) {
  const [teamFilter, setTeamFilter] = useState<string | null>(null);
  const [sevFilter, setSevFilter] = useState<string | null>(null);
  const [envFilter, setEnvFilter] = useState<string | null>(null);
  const [currentPage, setCurrentPage] = useState(1);

  const teams = useMemo(() => [...new Set(reports.map((r) => r.team))].sort(), [reports]);
  const severities = useMemo(() => [...new Set(reports.map((r) => r.severity))].sort(), [reports]);
  const environments = useMemo(() => [...new Set(reports.map((r) => r.environment))].sort(), [reports]);

  const filtered = useMemo(() => {
    let result = reports;
    if (teamFilter) result = result.filter((r) => r.team === teamFilter);
    if (sevFilter) result = result.filter((r) => r.severity === sevFilter);
    if (envFilter) result = result.filter((r) => r.environment === envFilter);
    return result;
  }, [reports, teamFilter, sevFilter, envFilter]);

  const filteredMet = filtered.filter((r) => r.sla_met === true).length;
  const filteredBreached = filtered.filter((r) => r.sla_met === false).length;
  const filteredRate = filtered.length > 0 ? Math.round((filteredMet / filtered.length) * 100 * 10) / 10 : 0;
  const paged = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  const totalPages = Math.ceil(filtered.length / PAGE_SIZE);

  if (loading) return null;
  if (!data || !data.total_reports) {
    return (
      <Card>
        <CardContent className="p-6">
          <div className="text-center py-6">
            <div className="text-3xl mb-2">📊</div>
            <p className="text-sm font-medium text-gray-700">No compliance data yet</p>
            <p className="text-xs text-gray-500 mt-1">Run a patching workflow to generate compliance reports</p>
          </div>
        </CardContent>
      </Card>
    );
  }

  const barColor = data.sla_rate_percent >= 95 ? "bg-emerald-500" : data.sla_rate_percent >= 80 ? "bg-amber-500" : "bg-red-500";

  return (
    <div className="space-y-4">
      {/* SLA progress bar */}
      <Card>
        <CardContent className="p-4">
          <h3 className="text-base font-semibold text-gray-900 mb-1">SLA Compliance — Last 30 Days</h3>
          <p className="text-xs text-gray-500 mb-3">{data.sla_met} met / {data.sla_breached} breached out of {data.total_reports} total reports</p>
        <div className="w-full bg-gray-200 rounded-full h-3">
          <div className={`h-3 rounded-full transition-all ${barColor}`} style={{ width: `${data.sla_rate_percent}%` }} />
        </div>
          <div className="text-right text-sm font-semibold mt-1">{data.sla_rate_percent}%</div>
        </CardContent>
      </Card>

      {/* Filters */}
      <div className="flex flex-wrap gap-2 items-center">
        <FilterSelect label="All teams" value={teamFilter} options={teams} onChange={(v) => { setTeamFilter(v); setCurrentPage(1); }} />
        <FilterSelect label="All severities" value={sevFilter} options={severities} onChange={(v) => { setSevFilter(v); setCurrentPage(1); }} />
        <FilterSelect label="All environments" value={envFilter} options={environments} onChange={(v) => { setEnvFilter(v); setCurrentPage(1); }} />
        {(teamFilter || sevFilter || envFilter) && (
          <button onClick={() => { setTeamFilter(null); setSevFilter(null); setEnvFilter(null); setCurrentPage(1); }} className="text-xs text-blue-600 hover:text-blue-800 transition">
            Clear filters
          </button>
        )}
        {filtered.length !== reports.length && (
          <span className="text-xs text-gray-500 ml-auto">
            {filtered.length} of {reports.length} · {filteredRate}% SLA · {filteredMet} met, {filteredBreached} breached
          </span>
        )}
      </div>

      {/* Reports table */}
      <div className="overflow-x-auto rounded-lg border border-gray-200">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-gray-50/80 border-b border-gray-200">
              {["When", "CVE", "Env", "Team", "Severity", "Decision", "Framework", "Patch Time", "SLA", "Result"].map((h) => (
                <th key={h} className="text-left text-[11px] font-semibold text-gray-500 uppercase tracking-wide py-2.5 px-3">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {paged.map((item) => (
              <tr key={item.report_id} className="hover:bg-blue-50/30 transition-colors">
                <td className="py-2.5 px-3 text-xs text-gray-500">{parseTimestamp(item.timestamp).toLocaleString()}</td>
                <td className="py-2.5 px-3 text-blue-600 font-medium">{item.cve_id}</td>
                <td className="py-2.5 px-3 text-gray-700">{item.environment}</td>
                <td className="py-2.5 px-3 text-gray-700">{item.team}</td>
                <td className="py-2.5 px-3">
                  <Badge variant={sevVariant(item.severity)}>{item.severity}</Badge>
                </td>
                <td className="py-2.5 px-3">
                  <Badge variant={decisionVariant(item.decision)}>{item.decision}</Badge>
                </td>
                <td className="py-2.5 px-3 text-gray-700 text-xs">{item.sla_source}</td>
                <td className="py-2.5 px-3 text-gray-700">{item.time_to_patch_hours != null ? `${item.time_to_patch_hours}h` : "—"}</td>
                <td className="py-2.5 px-3">
                  <SlaBadge met={item.sla_met} />
                </td>
                <td className="py-2.5 px-3">
                  <Badge variant={item.status === "Success" ? "success" : "destructive"}>
                    {item.success_count}/{item.instance_count}
                  </Badge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {filtered.length === 0 && (
          <div className="text-center text-gray-500 py-6 text-sm">No reports match filters</div>
        )}
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between">
          <span className="text-xs text-gray-500">Page {currentPage} of {totalPages}</span>
          <div className="flex gap-1">
            <PagBtn onClick={() => setCurrentPage((p) => Math.max(1, p - 1))} disabled={currentPage === 1}><ChevronLeftIcon className="w-4 h-4" /></PagBtn>
            <PagBtn onClick={() => setCurrentPage((p) => Math.min(totalPages, p + 1))} disabled={currentPage === totalPages}><ChevronRightIcon className="w-4 h-4" /></PagBtn>
          </div>
        </div>
      )}
    </div>
  );
}

function PagBtn({ children, onClick, disabled }: { children: React.ReactNode; onClick: () => void; disabled: boolean }) {
  return (
    <button onClick={onClick} disabled={disabled} className="p-1.5 rounded-lg border border-gray-200 hover:bg-gray-50 disabled:opacity-30 disabled:cursor-not-allowed transition text-gray-600">
      {children}
    </button>
  );
}

function FilterSelect({ label, value, options, onChange }: { label: string; value: string | null; options: string[]; onChange: (v: string | null) => void }) {
  return (
    <select
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value || null)}
      className="text-sm border border-gray-200 rounded-lg px-2.5 py-1.5 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent shadow-sm hover:border-gray-300 transition min-w-[140px]"
    >
      <option value="">{label}</option>
      {options.map((o) => <option key={o} value={o}>{o}</option>)}
    </select>
  );
}
