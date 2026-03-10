import { useState, useMemo } from "react";
import { ChevronLeftIcon, ChevronRightIcon, ChevronUpDownIcon, MagnifyingGlassIcon } from "@heroicons/react/24/outline";
import { Badge, sevVariant } from "./ui/Badge";
import { TableSkeleton } from "./ui/Skeleton";
import type { VulnerabilityFinding } from "../lib/api";

const PAGE_SIZE = 15;
const SEVERITY_OPTIONS = ["ALL", "CRITICAL", "HIGH", "MEDIUM", "LOW"];

interface Props {
  items: VulnerabilityFinding[];
  severityCounts: Record<string, number>;
  loading: boolean;
  onCveClick?: (cveId: string, env: string) => void;
}

export default function VulnTable({ items, severityCounts: _severityCounts, loading, onCveClick }: Props) {
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

  return (
    <div className="space-y-3">
      {/* Header with search + filter */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h3 className="text-base font-semibold text-gray-900">Active Vulnerabilities</h3>
          <p className="text-xs text-gray-500 mt-0.5">{totalUnique} unique CVEs</p>
        </div>
        <div className="flex items-center gap-2">
          {/* CVE search */}
          <div className="relative">
            <MagnifyingGlassIcon className="w-4 h-4 absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400" />
            <input
              type="text"
              placeholder="Search CVE ID..."
              value={searchQuery}
              onChange={(e) => { setSearchQuery(e.target.value); setCurrentPage(1); }}
              className="text-sm border border-gray-200 rounded-lg pl-8 pr-3 py-1.5 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent shadow-sm hover:border-gray-300 transition w-48"
            />
          </div>
          {/* Severity filter */}
          <select
            value={severityFilter}
            onChange={(e) => { setSeverityFilter(e.target.value); setCurrentPage(1); }}
            className="text-sm border border-gray-200 rounded-lg px-3 py-1.5 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent shadow-sm hover:border-gray-300 transition"
          >
            {SEVERITY_OPTIONS.map((opt) => (
              <option key={opt} value={opt}>{opt === "ALL" ? "All severities" : opt}</option>
            ))}
          </select>
        </div>
      </div>

      {/* Table */}
      <div className="overflow-x-auto rounded-lg border border-gray-200">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-gray-50/80 border-b border-gray-200">
              <Th>CVE ID</Th>
              <Th>Severity</Th>
              <th
                scope="col"
                onClick={() => { setSortByCvss((v) => !v); setCurrentPage(1); }}
                className="text-left text-[11px] font-semibold text-gray-500 uppercase tracking-wide py-2.5 px-3 cursor-pointer hover:text-gray-700 select-none transition"
                aria-label={sortByCvss ? "CVSS score, sorted descending" : "CVSS score, click to sort"}
              >
                <span className="inline-flex items-center gap-1">
                  CVSS <ChevronUpDownIcon className={`w-3 h-3 ${sortByCvss ? "text-blue-600" : ""}`} aria-hidden="true" />
                </span>
              </th>
              <Th>Service</Th>
              <Th>Environments</Th>
              <Th>Instances</Th>
              <Th>Fix</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {paged.map((item, i) => (
              <tr key={`${item.cve_id}-${i}`} className="hover:bg-blue-50/30 transition-colors">
                <td className="py-2.5 px-3">
                  <button
                    onClick={() => onCveClick?.(item.cve_id, item.environment)}
                    className="text-blue-600 hover:text-blue-800 font-medium bg-transparent border-0 cursor-pointer p-0 transition"
                  >
                    {item.cve_id}
                  </button>
                </td>
                <td className="py-2.5 px-3">
                  <Badge variant={sevVariant(item.severity)}>{item.severity}</Badge>
                </td>
                <td className="py-2.5 px-3 text-gray-700 font-mono text-xs">{item.cvss_score ?? "—"}</td>
                <td className="py-2.5 px-3">
                  <div className="flex gap-1 flex-wrap">
                    {(item.services ?? []).map((s, j) => (
                      <span key={j} className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-600">{s}</span>
                    ))}
                  </div>
                </td>
                <td className="py-2.5 px-3 text-gray-700 text-xs">
                  {(item.environments ?? [item.environment]).join(", ")}
                </td>
                <td className="py-2.5 px-3 text-gray-700 font-medium">{item.instance_count ?? 1}</td>
                <td className="py-2.5 px-3">
                  {item.fix_available === "YES" ? (
                    <Badge variant="success">Available</Badge>
                  ) : (
                    <span className="text-gray-400 text-xs">No</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {filtered.length === 0 && (
        <div className="text-center text-gray-500 py-6 text-sm">
          {searchQuery ? `No CVEs matching "${searchQuery}"` : `No ${severityFilter === "ALL" ? "" : severityFilter.toLowerCase() + " "}vulnerabilities found`}
        </div>
      )}

      {totalPages > 1 && (
        <div className="flex items-center justify-between pt-1">
          <span className="text-xs text-gray-500">Page {currentPage} of {totalPages} · {filtered.length} CVEs</span>
          <div className="flex gap-1">
            <PagBtn onClick={() => setCurrentPage((p) => Math.max(1, p - 1))} disabled={currentPage === 1}><ChevronLeftIcon className="w-4 h-4" /></PagBtn>
            <PagBtn onClick={() => setCurrentPage((p) => Math.min(totalPages, p + 1))} disabled={currentPage === totalPages}><ChevronRightIcon className="w-4 h-4" /></PagBtn>
          </div>
        </div>
      )}
    </div>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return <th scope="col" className="text-left text-[11px] font-semibold text-gray-500 uppercase tracking-wide py-2.5 px-3">{children}</th>;
}

function PagBtn({ children, onClick, disabled }: { children: React.ReactNode; onClick: () => void; disabled: boolean }) {
  return (
    <button onClick={onClick} disabled={disabled} className="p-1.5 rounded-lg border border-gray-200 hover:bg-gray-50 disabled:opacity-30 disabled:cursor-not-allowed transition text-gray-600">
      {children}
    </button>
  );
}
