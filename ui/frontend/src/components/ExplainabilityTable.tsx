import { useState, Fragment } from "react";
import { ChevronLeftIcon, ChevronRightIcon, ChevronDownIcon, ChevronUpIcon } from "@heroicons/react/24/outline";
import { Badge, decisionVariant, SlaBadge } from "./ui/Badge";
import { TableSkeleton } from "./ui/Skeleton";
import { text } from "../lib/typography";
import type { ReportDetail } from "../lib/api";

function parseTimestamp(ts: string): Date {
  return new Date(ts.replace(/([+-]\d{2}:\d{2})Z$/, "$1"));
}

interface Props {
  items: ReportDetail[];
  loading: boolean;
  onCveClick?: (cve: string, env: string) => void;
  onEmptyAction?: () => void;
}

const PAGE_SIZE = 10;

export default function ExplainabilityTable({ items, loading, onCveClick, onEmptyAction }: Props) {
  const [currentPage, setCurrentPage] = useState(1);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const paged = items.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  const totalPages = Math.ceil(items.length / PAGE_SIZE);

  if (loading && items.length === 0) {
    return <TableSkeleton rows={5} cols={7} />;
  }

  return (
    <div className="space-y-3">
      <div>
        <h3 className={text.sectionTitle}>Decision Audit Trail</h3>
        <p className="text-xs text-fg-muted mt-0.5">Full audit trail of agent decisions &mdash; why each patch was applied, what SLA framework governed it, and the outcome.</p>
      </div>

      <div className="overflow-x-auto rounded-xl border border-edge">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-surface-raised border-b border-edge sticky top-0 z-10">
              {["When", "CVE", "Env", "Team", "Operator", "Decision", "SLA", ""].map((h) => (
                <th key={h} className="text-left text-[11px] font-semibold text-fg-muted uppercase tracking-wide py-2.5 px-3">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-white/[0.04]">
            {paged.map((item) => (
              <Fragment key={item.report_id}>
                <tr className="hover:bg-hover-row transition-colors">
                  <td className="py-2.5 px-3 text-xs text-fg-muted font-mono">{parseTimestamp(item.timestamp).toLocaleString()}</td>
                  <td className="py-2.5 px-3">
                    <button
                      onClick={() => onCveClick?.(item.cve_id, item.environment)}
                      className="text-accent hover:text-accent-hover font-semibold bg-transparent border-0 cursor-pointer p-0 transition font-mono text-xs"
                    >
                      {item.cve_id}
                    </button>
                  </td>
                  <td className="py-2.5 px-3 text-fg-muted">{item.environment}</td>
                  <td className="py-2.5 px-3 text-fg-muted">{item.team}</td>
                  <td className="py-2.5 px-3 text-xs text-fg-muted font-mono">{item.operator || "\u2014"}</td>
                  <td className="py-2.5 px-3">
                    <Badge variant={decisionVariant(item.decision)}>{item.decision}</Badge>
                  </td>
                  <td className="py-2.5 px-3">
                    <SlaBadge met={item.sla_met} />
                  </td>
                  <td className="py-2.5 px-3">
                    <button
                      onClick={() => setExpandedId(expandedId === item.report_id ? null : item.report_id)}
                      className="text-accent/70 hover:text-accent text-xs bg-transparent border-0 cursor-pointer p-0 flex items-center gap-0.5 transition"
                    >
                      {expandedId === item.report_id ? (
                        <><ChevronUpIcon className="w-3 h-3" /> Hide</>
                      ) : (
                        <><ChevronDownIcon className="w-3 h-3" /> Details</>
                      )}
                    </button>
                  </td>
                </tr>
                {expandedId === item.report_id && (
                  <tr>
                    <td colSpan={8} className="p-0">
                      <DecisionDetail item={item} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
        {items.length === 0 && (
          <div className="text-center py-10">
            <svg className="w-8 h-8 text-fg-faint mx-auto mb-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" /></svg>
            <p className="text-sm font-medium text-fg-secondary">No patching decisions recorded yet</p>
            <p className="text-xs text-fg-muted mt-1">Complete a patch workflow to see decision audit trails</p>
            {onEmptyAction && (
              <button
                onClick={onEmptyAction}
                className="px-3 py-1.5 text-xs font-medium text-accent bg-accent-muted border border-accent-border rounded-lg hover:bg-accent/20 transition mt-3"
              >
                Start patching
              </button>
            )}
          </div>
        )}
      </div>

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

function DecisionDetail({ item }: { item: ReportDetail }) {
  return (
    <div className="bg-teal-400/[0.03] border-t border-b border-teal-400/10 px-6 py-4">
      <h4 className="text-sm font-semibold text-fg mb-3">Decision Reasoning &mdash; {item.cve_id} in {item.environment}</h4>
      <div className="grid grid-cols-2 gap-x-8 gap-y-3 text-sm">
        <DetailRow label="Vulnerability" value={`${item.cve_id} (${item.severity}${item.cvss_score ? ` / CVSS ${item.cvss_score}` : ""})`} />
        <DetailRow label="Decision Type">
          <Badge variant={decisionVariant(item.decision)}>{item.decision}</Badge>
          <span className="text-xs text-fg-muted ml-2">
            {item.decision === "EMERGENCY" ? "Severity or SLA urgency triggered immediate patching" : "Scheduled within maintenance window per SLA policy"}
          </span>
        </DetailRow>
        <DetailRow label="Compliance Framework" value={
          item.frameworks && item.frameworks.length > 0
            ? item.frameworks.join(", ")
            : "—"
        } />
        <DetailRow label="Scope" value={`${item.instance_count} instances \u2014 Team: ${item.team}, Product: ${item.product}`} />
        <DetailRow label="SLA Threshold" value={item.sla_hours != null ? `${item.sla_hours} hours` : "N/A"} />
        <DetailRow label="Execution Result">
          <Badge variant={item.status === "Success" ? "success" : "destructive"}>{item.status}</Badge>
          <span className="text-xs text-fg-muted ml-1">&mdash; {item.success_count} succeeded, {item.failure_count} failed</span>
        </DetailRow>
        <DetailRow label="Initiated By" value={item.operator || "unknown"} />
        <DetailRow label="Report ID" value={item.report_id} className="text-fg-faint text-xs font-mono" />
      </div>
    </div>
  );
}

function DetailRow({ label, value, children, className }: { label: string; value?: string; children?: React.ReactNode; className?: string }) {
  return (
    <div>
      <div className="text-[11px] font-semibold text-fg-muted uppercase tracking-wide mb-0.5">{label}</div>
      <div className={className ?? "text-fg-secondary"}>{children ?? value}</div>
    </div>
  );
}
