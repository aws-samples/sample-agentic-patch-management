import { useMemo, useState } from "react";
import { ChevronLeftIcon, ChevronRightIcon } from "@heroicons/react/24/outline";
import { Badge, sevVariant, decisionVariant, SlaBadge } from "./ui/Badge";
import { Card, CardContent } from "./ui/Card";
import { text } from "../lib/typography";
import type { ComplianceSummary as ComplianceData, ReportDetail } from "../lib/api";

function parseTimestamp(ts: string): Date {
  return new Date(ts.replace(/([+-]\d{2}:\d{2})Z$/, "$1"));
}

interface Props {
  data: ComplianceData | null;
  reports: ReportDetail[];
  loading: boolean;
  onEmptyAction?: () => void;
}

const PAGE_SIZE = 10;

export default function ComplianceSummary({ data, reports, loading, onEmptyAction }: Props) {
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
            <svg className="w-8 h-8 text-fg-faint mx-auto mb-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" /></svg>
            <p className="text-sm font-medium text-fg-secondary">No compliance data yet</p>
            <p className="text-xs text-fg-muted mt-1">Run a patch workflow to start tracking compliance</p>
            {onEmptyAction && (
              <button
                onClick={onEmptyAction}
                className="px-3 py-1.5 text-xs font-medium text-accent bg-accent-muted border border-accent-border rounded-lg hover:bg-accent/20 transition mt-3"
              >
                Patch an environment
              </button>
            )}
          </div>
        </CardContent>
      </Card>
    );
  }

  const barColor = data.sla_rate_percent >= 95 ? "bg-emerald-400" : data.sla_rate_percent >= 80 ? "bg-amber-400" : "bg-red-400";

  return (
    <div className="space-y-4">
      {/* SLA progress bar */}
      <Card>
        <CardContent className="p-4">
          <h3 className={`${text.sectionTitle} mb-1`}>SLA Compliance &mdash; Last 30 Days</h3>
          <p className="text-xs text-fg-muted mb-3 font-mono">
            {data.sla_met} met / {data.sla_breached} breached
            {(data.total_reports - data.sla_met - data.sla_breached) > 0
              ? ` / ${data.total_reports - data.sla_met - data.sla_breached} unknown`
              : ""}
            {" "}of {data.total_reports} total reports
          </p>
          <div className="w-full bg-hover rounded-full h-3">
            <div className={`h-3 rounded-full transition-all ${barColor}`} style={{ width: `${data.sla_rate_percent}%` }} />
          </div>
          <div className="text-right text-sm font-semibold mt-1 text-fg font-mono">{data.sla_rate_percent}%</div>
        </CardContent>
      </Card>

      {/* Filters */}
      <div className="flex flex-wrap gap-2 items-center">
        <FilterSelect label="All teams" value={teamFilter} options={teams} onChange={(v) => { setTeamFilter(v); setCurrentPage(1); }} />
        <FilterSelect label="All severities" value={sevFilter} options={severities} onChange={(v) => { setSevFilter(v); setCurrentPage(1); }} />
        <FilterSelect label="All environments" value={envFilter} options={environments} onChange={(v) => { setEnvFilter(v); setCurrentPage(1); }} />
        {(teamFilter || sevFilter || envFilter) && (
          <button onClick={() => { setTeamFilter(null); setSevFilter(null); setEnvFilter(null); setCurrentPage(1); }} className="text-xs text-accent hover:text-accent-hover transition">
            Clear filters
          </button>
        )}
        {filtered.length !== reports.length && (
          <span className="text-xs text-fg-muted ml-auto font-mono">
            {filtered.length} of {reports.length} &middot; {filteredRate}% SLA &middot; {filteredMet} met, {filteredBreached} breached
          </span>
        )}
      </div>

      {/* Reports table */}
      <div className="overflow-x-auto rounded-xl border border-edge">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-surface-raised border-b border-edge">
              {["When", "CVE", "Env", "Team", "Severity", "Decision", "Framework", "SLA", "Result"].map((h) => (
                <th key={h} className="text-left text-[11px] font-semibold text-fg-muted uppercase tracking-wide py-2.5 px-3">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-white/[0.04]">
            {paged.map((item) => (
              <tr key={item.report_id} className="hover:bg-hover-row transition-colors">
                <td className="py-2.5 px-3 text-xs text-fg-muted font-mono">{parseTimestamp(item.timestamp).toLocaleString()}</td>
                <td className="py-2.5 px-3 text-accent font-medium font-mono text-xs">{item.cve_id}</td>
                <td className="py-2.5 px-3 text-fg-muted">{item.environment}</td>
                <td className="py-2.5 px-3 text-fg-muted">{item.team}</td>
                <td className="py-2.5 px-3">
                  <Badge variant={sevVariant(item.severity)}>{item.severity}</Badge>
                </td>
                <td className="py-2.5 px-3">
                  <Badge variant={decisionVariant(item.decision)}>{item.decision}</Badge>
                </td>
                <td className="py-2.5 px-3 text-fg-muted text-xs font-mono">
                  {item.frameworks && item.frameworks.length > 0
                    ? item.frameworks.join(", ")
                    : <span className="text-fg-faint">&mdash;</span>}
                </td>
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
          <div className="text-center text-fg-muted py-6 text-sm">No reports match filters</div>
        )}
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between">
          <span className="text-xs text-fg-muted font-mono">Page {currentPage} of {totalPages}</span>
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
    <button onClick={onClick} disabled={disabled} className="p-1.5 rounded-lg border border-edge hover:bg-hover disabled:opacity-30 disabled:cursor-not-allowed transition text-fg-muted">
      {children}
    </button>
  );
}

function FilterSelect({ label, value, options, onChange }: { label: string; value: string | null; options: string[]; onChange: (v: string | null) => void }) {
  return (
    <select
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value || null)}
      className="text-sm bg-input border border-edge rounded-lg px-2.5 py-1.5 text-fg-secondary focus:outline-none focus:ring-2 focus:ring-accent-border focus:border-accent-border transition min-w-[140px]"
    >
      <option value="" className="bg-surface-raised">{label}</option>
      {options.map((o) => <option key={o} value={o} className="bg-surface-raised">{o}</option>)}
    </select>
  );
}
