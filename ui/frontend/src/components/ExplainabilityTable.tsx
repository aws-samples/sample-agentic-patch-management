import { useState } from "react";
import { ChevronLeftIcon, ChevronRightIcon, ChevronDownIcon, ChevronUpIcon } from "@heroicons/react/24/outline";
import { Badge, decisionVariant, SlaBadge } from "./ui/Badge";
import { TableSkeleton } from "./ui/Skeleton";
import type { ReportDetail } from "../lib/api";

/** Parse timestamps that may have a trailing Z after a UTC offset (e.g. +00:00Z) */
function parseTimestamp(ts: string): Date {
  return new Date(ts.replace(/([+-]\d{2}:\d{2})Z$/, "$1"));
}

interface Props {
  items: ReportDetail[];
  loading: boolean;
  onCveClick?: (cve: string, env: string) => void;
}

const PAGE_SIZE = 10;

export default function ExplainabilityTable({ items, loading, onCveClick }: Props) {
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
        <h3 className="text-base font-semibold text-gray-900">Decision Audit Trail</h3>
        <p className="text-xs text-gray-500 mt-0.5">Full audit trail of agent decisions — why each patch was applied, what SLA framework governed it, and the outcome.</p>
      </div>

      <div className="overflow-x-auto rounded-lg border border-gray-200">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-gray-50/80 border-b border-gray-200">
              {["When", "CVE", "Env", "Team", "Operator", "Decision", "SLA", ""].map((h) => (
                <th key={h} className="text-left text-[11px] font-semibold text-gray-500 uppercase tracking-wide py-2.5 px-3">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {paged.map((item) => (
              <>
                <tr key={item.report_id} className="hover:bg-blue-50/30 transition-colors">
                  <td className="py-2.5 px-3 text-xs text-gray-500">{parseTimestamp(item.timestamp).toLocaleString()}</td>
                  <td className="py-2.5 px-3">
                    <button
                      onClick={() => onCveClick?.(item.cve_id, item.environment)}
                      className="text-blue-600 hover:text-blue-800 font-semibold bg-transparent border-0 cursor-pointer p-0 transition"
                    >
                      {item.cve_id}
                    </button>
                  </td>
                  <td className="py-2.5 px-3 text-gray-700">{item.environment}</td>
                  <td className="py-2.5 px-3 text-gray-700">{item.team}</td>
                  <td className="py-2.5 px-3 text-xs text-gray-500">{item.operator || "—"}</td>
                  <td className="py-2.5 px-3">
                    <Badge variant={decisionVariant(item.decision)}>{item.decision}</Badge>
                  </td>
                  <td className="py-2.5 px-3">
                    <SlaBadge met={item.sla_met} />
                  </td>
                  <td className="py-2.5 px-3">
                    <button
                      onClick={() => setExpandedId(expandedId === item.report_id ? null : item.report_id)}
                      className="text-blue-600 hover:text-blue-800 text-xs bg-transparent border-0 cursor-pointer p-0 flex items-center gap-0.5 transition"
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
                  <tr key={`${item.report_id}-detail`}>
                    <td colSpan={8} className="p-0">
                      <DecisionDetail item={item} />
                    </td>
                  </tr>
                )}
              </>
            ))}
          </tbody>
        </table>
        {items.length === 0 && (
          <div className="text-center py-10">
            <div className="text-3xl mb-2">🔍</div>
            <p className="text-sm font-medium text-gray-700">No patching decisions recorded yet</p>
            <p className="text-xs text-gray-500 mt-1">Decisions will appear here after running patch workflows</p>
          </div>
        )}
      </div>

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

function DecisionDetail({ item }: { item: ReportDetail }) {
  return (
    <div className="bg-blue-50/30 border-t border-b border-blue-100 px-6 py-4">
      <h4 className="text-sm font-semibold text-gray-900 mb-3">Decision Reasoning — {item.cve_id} in {item.environment}</h4>
      <div className="grid grid-cols-2 gap-x-8 gap-y-3 text-sm">
        <DetailRow label="Vulnerability" value={`${item.cve_id} (${item.severity}${item.cvss_score ? ` / CVSS ${item.cvss_score}` : ""})`} />
        <DetailRow label="Decision Type">
          <Badge variant={decisionVariant(item.decision)}>{item.decision}</Badge>
          <span className="text-xs text-gray-500 ml-2">
            {item.decision === "EMERGENCY" ? "Severity or SLA urgency triggered immediate patching" : "Scheduled within maintenance window per SLA policy"}
          </span>
        </DetailRow>
        <DetailRow label="Compliance Framework" value={item.sla_source} />
        <DetailRow label="Scope" value={`${item.instance_count} instances — Team: ${item.team}, Product: ${item.product}`} />
        <DetailRow label="SLA Threshold" value={item.sla_hours != null ? `${item.sla_hours} hours` : "N/A"} />
        <DetailRow label="Execution Result">
          <Badge variant={item.status === "Success" ? "success" : "destructive"}>{item.status}</Badge>
          <span className="text-xs text-gray-500 ml-1">— {item.success_count} succeeded, {item.failure_count} failed</span>
        </DetailRow>
        <DetailRow label="Time to Patch">
          {item.time_to_patch_hours != null ? (
            <>
              <span>{item.time_to_patch_hours} hours</span>
              {item.sla_hours != null && (
                <Badge variant={item.time_to_patch_hours <= item.sla_hours ? "success" : "destructive"} className="ml-2">
                  {item.time_to_patch_hours <= item.sla_hours ? "✓ Within SLA" : "✗ SLA Exceeded"}
                </Badge>
              )}
            </>
          ) : "N/A"}
        </DetailRow>
        <DetailRow label="Initiated By" value={item.operator || "unknown"} />
        <DetailRow label="Report ID" value={item.report_id} className="text-gray-400 text-xs" />
      </div>
    </div>
  );
}

function DetailRow({ label, value, children, className }: { label: string; value?: string; children?: React.ReactNode; className?: string }) {
  return (
    <div>
      <div className="text-[11px] font-semibold text-gray-500 uppercase tracking-wide mb-0.5">{label}</div>
      <div className={className ?? "text-gray-900"}>{children ?? value}</div>
    </div>
  );
}
