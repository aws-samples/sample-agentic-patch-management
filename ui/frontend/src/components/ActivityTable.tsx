import { Badge, decisionVariant, SlaBadge } from "./ui/Badge";
import { TableSkeleton } from "./ui/Skeleton";
import type { ActivityItem } from "../lib/api";

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(iso).toLocaleDateString();
}

interface Props {
  items: ActivityItem[];
  loading: boolean;
  onRowClick?: (cveId: string, environment: string) => void;
}

export default function ActivityTable({ items, loading, onRowClick }: Props) {
  const recent = items.slice(0, 10);

  if (loading && items.length === 0) {
    return <TableSkeleton rows={5} cols={5} />;
  }

  return (
    <div>
      <h3 className="text-base font-semibold text-gray-900 mb-3">Recent Activity</h3>
      <div className="overflow-x-auto rounded-lg border border-gray-200">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-gray-50/80 border-b border-gray-200">
              <Th>When</Th>
              <Th>CVE</Th>
              <Th>Env</Th>
              <Th>Type</Th>
              <Th>SLA</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {recent.map((item, i) => (
              <tr key={`${item.cve_id}-${item.timestamp}-${i}`} className="hover:bg-blue-50/30 transition-colors">
                <td className="py-2.5 px-3 text-xs text-gray-500">{relativeTime(item.timestamp)}</td>
                <td className="py-2.5 px-3">
                  <button
                    onClick={() => onRowClick?.(item.cve_id, item.environment)}
                    className="text-blue-600 hover:text-blue-800 font-medium bg-transparent border-0 cursor-pointer p-0 transition"
                  >
                    {item.cve_id}
                  </button>
                </td>
                <td className="py-2.5 px-3 text-gray-700">{item.environment}</td>
                <td className="py-2.5 px-3">
                  <Badge variant={decisionVariant(item.decision)}>{item.decision}</Badge>
                </td>
                <td className="py-2.5 px-3">
                  <SlaBadge met={item.sla_met} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {recent.length === 0 && (
          <div className="text-center py-10">
            <div className="text-3xl mb-2">📋</div>
            <p className="text-sm font-medium text-gray-700">No patching activity yet</p>
            <p className="text-xs text-gray-500 mt-1">Activity will appear here after running patch workflows</p>
          </div>
        )}
      </div>
    </div>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return <th className="text-left text-[11px] font-semibold text-gray-500 uppercase tracking-wide py-2.5 px-3">{children}</th>;
}
