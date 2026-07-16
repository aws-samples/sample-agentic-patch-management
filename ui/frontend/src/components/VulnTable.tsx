import { useState, useMemo } from "react";
import { ChevronLeftIcon, ChevronRightIcon, ChevronUpDownIcon, MagnifyingGlassIcon } from "@heroicons/react/24/outline";
import { Badge, sevVariant } from "./ui/Badge";
import { TableSkeleton } from "./ui/Skeleton";
import { text } from "../lib/typography";
import type { VulnerabilityFinding } from "../lib/api";

const PAGE_SIZE = 15;
const SEVERITY_OPTIONS = ["ALL", "CRITICAL", "HIGH", "MEDIUM", "LOW"];

interface Props {
  items: VulnerabilityFinding[];
  severityCounts: Record<string, number>;
  loading: boolean;
  onCveClick?: (cveId: string, env: string) => void;
  onEmptyAction?: () => void;
}

export default function VulnTable({ items, severityCounts: _severityCounts, loading, onCveClick, onEmptyAction }: Props) {
  const [currentPage, setCurrentPage] = useState(1);
  const [severityFilter, setSeverityFilter] = useState("ALL");
  const [searchQuery, setSearchQuery] = useState("");
  const [sortByCvss, setSortByCvss] = useState(false);

  const filtered = useMemo(() => {
    let result = items;
    if (severityFilter !== "ALL") result = result.filter((i) => i.severity === severityFilter);
    if (searchQuery.trim()) {
      const q = searchQuery.trim().toUpperCase();
      result = result.filter((i) => i.cve_id.toUpperCase().includes(q));
    }
    if (sortByCvss) result = [...result].sort((a, b) => (b.cvss_score ?? 0) - (a.cvss_score ?? 0));
    return result;
  }, [items, severityFilter, searchQuery, sortByCvss]);

  const paged = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  const totalPages = Math.ceil(filtered.length / PAGE_SIZE);
  const totalUnique = items.length;

  if (loading && items.length === 0) return <TableSkeleton rows={6} cols={7} />;

  if (items.length === 0) {
    return (
      <div className="text-center py-10">
        <svg className="w-8 h-8 text-fg-faint mx-auto mb-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" /></svg>
        <p className="text-sm font-medium text-fg-secondary">No vulnerabilities detected</p>
        <p className="text-xs text-fg-muted mt-1">Scan your fleet to discover active CVEs</p>
        {onEmptyAction && (
          <button
            onClick={onEmptyAction}
            className="px-3 py-1.5 text-xs font-medium text-accent bg-accent-muted border border-accent-border rounded-lg hover:bg-accent/20 transition mt-3"
          >
            Scan for vulnerabilities
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {/* Header with search + filter */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h3 className={text.sectionTitle}>Active Vulnerabilities</h3>
          <p className="text-xs text-fg-muted mt-0.5 font-mono">{totalUnique} unique CVEs</p>
        </div>
        <div className="flex items-center gap-2">
          <div className="relative">
            <MagnifyingGlassIcon className="w-4 h-4 absolute left-2.5 top-1/2 -translate-y-1/2 text-fg-muted" />
            <input
              type="text"
              placeholder="Search CVE ID..."
              aria-label="Search vulnerabilities by CVE ID"
              value={searchQuery}
              onChange={(e) => { setSearchQuery(e.target.value); setCurrentPage(1); }}
              className="text-sm bg-input border border-edge rounded-lg pl-8 pr-3 py-1.5 text-fg placeholder:text-fg-faint focus:outline-none focus:ring-2 focus:ring-accent-border focus:border-accent-border transition w-48"
            />
          </div>
          <select
            value={severityFilter}
            aria-label="Filter by severity"
            onChange={(e) => { setSeverityFilter(e.target.value); setCurrentPage(1); }}
            className="text-sm bg-input border border-edge rounded-lg px-3 py-1.5 text-fg-secondary focus:outline-none focus:ring-2 focus:ring-accent-border focus:border-accent-border transition"
          >
            {SEVERITY_OPTIONS.map((opt) => (
              <option key={opt} value={opt} className="bg-surface-raised">{opt === "ALL" ? "All severities" : opt}</option>
            ))}
          </select>
        </div>
      </div>

      {/* Table */}
      <div className="overflow-x-auto rounded-xl border border-edge">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-surface-raised border-b border-edge sticky top-0 z-10">
              <Th className="sticky left-0 bg-surface-raised z-20">CVE ID</Th>
              <Th>Severity</Th>
              <th
                onClick={() => { setSortByCvss((v) => !v); setCurrentPage(1); }}
                className="text-left text-[11px] font-semibold text-fg-muted uppercase tracking-wide py-2.5 px-3 cursor-pointer hover:text-fg-secondary select-none transition"
              >
                <span className="inline-flex items-center gap-1">
                  CVSS <ChevronUpDownIcon className={`w-3 h-3 ${sortByCvss ? "text-accent" : ""}`} />
                </span>
              </th>
              <Th>Service</Th>
              <Th>Environments</Th>
              <Th>Instances</Th>
              <Th>Fix</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-white/[0.04]">
            {paged.map((item, i) => (
              <tr key={`${item.cve_id}-${i}`} className="hover:bg-hover-row transition-colors">
                <td className="py-2.5 px-3">
                  <button
                    onClick={() => onCveClick?.(item.cve_id, item.environment)}
                    className="text-accent hover:text-accent-hover font-medium bg-transparent border-0 cursor-pointer p-0 transition font-mono text-xs"
                  >
                    {item.cve_id}
                  </button>
                </td>
                <td className="py-2.5 px-3">
                  <Badge variant={sevVariant(item.severity)}>{item.severity}</Badge>
                </td>
                <td className="py-2.5 px-3 text-fg-secondary font-mono text-xs">{item.cvss_score ?? "\u2014"}</td>
                <td className="py-2.5 px-3">
                  <div className="flex gap-1 flex-wrap">
                    {(item.services ?? []).map((s, j) => (
                      <span key={j} className="text-[10px] px-1.5 py-0.5 rounded bg-hover text-fg-muted font-mono">{s}</span>
                    ))}
                  </div>
                </td>
                <td className="py-2.5 px-3 text-fg-muted text-xs">
                  {(item.environments ?? [item.environment]).join(", ")}
                  {(item.accounts?.length ?? 0) > 1 && (
                    <span className="ml-1.5 text-[9px] px-1 py-px rounded bg-teal-500/10 text-teal-400 font-mono">
                      {item.accounts.length} accts
                    </span>
                  )}
                </td>
                <td className="py-2.5 px-3 text-fg-secondary font-medium font-mono">{item.instance_count ?? 1}</td>
                <td className="py-2.5 px-3">
                  {item.fix_available === "YES" ? (
                    <Badge variant="success">Available</Badge>
                  ) : (
                    <span className="text-fg-faint text-xs">No</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {filtered.length === 0 && (
        <div className="text-center text-fg-muted py-6 text-sm">
          {searchQuery ? `No CVEs matching "${searchQuery}"` : `No ${severityFilter === "ALL" ? "" : severityFilter.toLowerCase() + " "}vulnerabilities found`}
        </div>
      )}

      {totalPages > 1 && (
        <div className="flex items-center justify-between pt-1">
          <span className="text-xs text-fg-muted font-mono">Page {currentPage} of {totalPages} &middot; {filtered.length} CVEs</span>
          <div className="flex gap-1">
            <PagBtn onClick={() => setCurrentPage((p) => Math.max(1, p - 1))} disabled={currentPage === 1}><ChevronLeftIcon className="w-4 h-4" /></PagBtn>
            <PagBtn onClick={() => setCurrentPage((p) => Math.min(totalPages, p + 1))} disabled={currentPage === totalPages}><ChevronRightIcon className="w-4 h-4" /></PagBtn>
          </div>
        </div>
      )}
    </div>
  );
}

function Th({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <th className={`text-left text-[11px] font-semibold text-fg-muted uppercase tracking-wide py-2.5 px-3 ${className}`}>{children}</th>;
}

function PagBtn({ children, onClick, disabled }: { children: React.ReactNode; onClick: () => void; disabled: boolean }) {
  return (
    <button onClick={onClick} disabled={disabled} className="p-1.5 rounded-lg border border-edge hover:bg-hover disabled:opacity-30 disabled:cursor-not-allowed transition text-fg-muted">
      {children}
    </button>
  );
}
