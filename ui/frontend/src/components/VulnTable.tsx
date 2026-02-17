import { useState, useMemo } from "react";
import { ChevronLeftIcon, ChevronRightIcon, ChevronUpDownIcon } from "@heroicons/react/24/outline";
import { Badge, sevVariant } from "./ui/Badge";
import { TableSkeleton } from "./ui/Skeleton";
import type { VulnerabilityFinding } from "../lib/api";

const PAGE_SIZE = 10;
const SEVERITY_OPTIONS = ["ALL", "CRITICAL", "HIGH", "MEDIUM", "LOW"];

interface Props {
  items: VulnerabilityFinding[];
  severityCounts: Record<string, number>;
  loading: boolean;
  onCveClick?: (cveId: string, env: string) => void;
}

export default function VulnTable({ items, severityCounts, loading, onCveClick }: Props) {
  const [currentPage, setCurrentPage] = useState(1);
  const [severityFilter, setSeverityFilter] = useState("ALL");
  const [sortByCvss, setSortByCvss] = useState(false);

  const filtered = useMemo(() => {
    let result = severityFilter === "ALL" ? items : items.filter((i) => i.severity === severityFilter);
    if (sortByCvss) result = [...result].sort((a, b) => (b.cvss_score ?? 0) - (a.cvss_score ?? 0));
    return result;
  }, [items, severityFilter, sortByCvss]);

  const paged = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  const totalPages = Math.ceil(filtered.length / PAGE_SIZE);

  const counterParts: string[] = [];
  for (const sev of ["CRITICAL", "HIGH", "MEDIUM", "LOW"]) {
    const count = severityCounts[sev];
    if (count) counterParts.push(`${count} ${sev[0]}${sev.slice(1).toLowerCase()}`);
  }
  const counter = counterParts.length > 0 ? counterParts.join(", ") : `${items.length}`;
  const totalFromCounts = Object.values(severityCounts).reduce((s, n) => s + n, 0);
  const isTruncated = totalFromCounts > items.length;

  if (loading && items.length === 0) {
    return <TableSkeleton rows={6} cols={5} />;
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-base font-semibold text-gray-900">Active Vulnerabilities</h3>
          <p className="text-xs text-gray-500 mt-0.5">{counter}</p>
        </div>
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

      <div className="overflow-x-auto rounded-lg border border-gray-200">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-gray-50/80 border-b border-gray-200">
              <Th>CVE ID</Th>
              <Th>Severity</Th>
              <th
                onClick={() => { setSortByCvss((v) => !v); setCurrentPage(1); }}
                className="text-left text-[11px] font-semibold text-gray-500 uppercase tracking-wide py-2.5 px-3 cursor-pointer hover:text-gray-700 select-none transition"
              >
                <span className="inline-flex items-center gap-1">
                  CVSS <ChevronUpDownIcon className={`w-3 h-3 ${sortByCvss ? "text-blue-600" : ""}`} />
                </span>
              </th>
              <Th>Environment</Th>
              <Th>Fix</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {paged.map((item, i) => (
              <tr key={`${item.cve_id}-${item.resource_id}-${i}`} className="hover:bg-blue-50/30 transition-colors">
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
                <td className="py-2.5 px-3 text-gray-700">{item.environment}</td>
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
          {isTruncated && severityFilter !== "ALL" && (severityCounts[severityFilter] ?? 0) > 0
            ? `${severityCounts[severityFilter]} ${severityFilter.toLowerCase()} findings are not in the loaded sample (${items.length} of ${totalFromCounts} loaded). Use the chat to query: "Show ${severityFilter.toLowerCase()} vulnerabilities"`
            : `No ${severityFilter === "ALL" ? "" : severityFilter.toLowerCase() + " "}vulnerabilities found`}
        </div>
      )}

      {totalPages > 1 && (
        <div className="flex items-center justify-between pt-1">
          <span className="text-xs text-gray-500">Page {currentPage} of {totalPages} · {filtered.length} results</span>
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
  return <th className="text-left text-[11px] font-semibold text-gray-500 uppercase tracking-wide py-2.5 px-3">{children}</th>;
}

function PagBtn({ children, onClick, disabled }: { children: React.ReactNode; onClick: () => void; disabled: boolean }) {
  return (
    <button onClick={onClick} disabled={disabled} className="p-1.5 rounded-lg border border-gray-200 hover:bg-gray-50 disabled:opacity-30 disabled:cursor-not-allowed transition text-gray-600">
      {children}
    </button>
  );
}
