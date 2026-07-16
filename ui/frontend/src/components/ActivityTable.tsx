import { Badge, decisionVariant, SlaBadge } from "./ui/Badge";
import { TableSkeleton } from "./ui/Skeleton";
import { text } from "../lib/typography";
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
  onEmptyAction?: () => void;
}

export default function ActivityTable({ items, loading, onRowClick, onEmptyAction }: Props) {
  const recent = items.slice(0, 10);

  if (loading && items.length === 0) {
    return <TableSkeleton rows={5} cols={5} />;
  }

  return (
    <div>
      <h3 className={`${text.sectionTitle} mb-3`}>Recent Activity</h3>
      <div className="overflow-x-auto rounded-xl border border-edge">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-surface-raised border-b border-edge sticky top-0 z-10">
              <Th>When</Th>
              <Th>Operation</Th>
              <Th>Env</Th>
              <Th>Detail</Th>
              <Th>Result</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-white/[0.04]">
            {recent.map((item, i) => (
              <tr key={`${item.report_key}-${item.timestamp}-${i}`} className="hover:bg-hover-row transition-colors">
                <td className="py-2.5 px-3 text-xs text-fg-muted font-mono">{relativeTime(item.timestamp)}</td>
                {item.operation_type === "rollback" ? (
                  <>
                    <td className="py-2.5 px-3">
                      <Badge variant="info">Rollback</Badge>
                    </td>
                    <td className="py-2.5 px-3 text-fg-muted">{item.environment}</td>
                    <td className="py-2.5 px-3 text-fg-muted text-xs">
                      {item.instance_count != null ? `${item.instance_count} instances` : "—"}
                    </td>
                    <td className="py-2.5 px-3">
                      <Badge variant={item.status === "Success" ? "success" : item.status === "Failed" ? "destructive" : "default"}>
                        {item.status ?? "—"}
                      </Badge>
                    </td>
                  </>
                ) : (
                  <>
                    <td className="py-2.5 px-3">
                      <button
                        onClick={() => item.cve_id && onRowClick?.(item.cve_id, item.environment)}
                        className="text-accent hover:text-accent-hover font-medium bg-transparent border-0 cursor-pointer p-0 transition font-mono text-xs"
                      >
                        {item.cve_id ?? "N/A"}
                      </button>
                    </td>
                    <td className="py-2.5 px-3 text-fg-muted">{item.environment}</td>
                    <td className="py-2.5 px-3">
                      {item.decision ? (
                        <Badge variant={decisionVariant(item.decision)}>{item.decision}</Badge>
                      ) : <span className="text-fg-faint text-xs">&mdash;</span>}
                    </td>
                    <td className="py-2.5 px-3">
                      <SlaBadge met={item.sla_met} />
                    </td>
                  </>
                )}
              </tr>
            ))}
          </tbody>
        </table>
        {recent.length === 0 && (
          <div className="text-center py-10">
            <svg className="w-8 h-8 text-fg-faint mx-auto mb-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M9 12h3.75M9 15h3.75M9 18h3.75m3 .75H18a2.25 2.25 0 002.25-2.25V6.108c0-1.135-.845-2.098-1.976-2.192a48.424 48.424 0 00-1.123-.08m-5.801 0c-.065.21-.1.433-.1.664 0 .414.336.75.75.75h4.5a.75.75 0 00.75-.75 2.25 2.25 0 00-.1-.664m-5.8 0A2.251 2.251 0 0113.5 2.25H15c1.012 0 1.867.668 2.15 1.586m-5.8 0c-.376.023-.75.05-1.124.08C9.095 4.01 8.25 4.973 8.25 6.108V8.25m0 0H4.875c-.621 0-1.125.504-1.125 1.125v11.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125V9.375c0-.621-.504-1.125-1.125-1.125H8.25z" /></svg>
            <p className="text-sm font-medium text-fg-secondary">No patching activity yet</p>
            <p className="text-xs text-fg-muted mt-1">Activity will appear here after running patch workflows</p>
            {onEmptyAction && (
              <button
                onClick={onEmptyAction}
                className="px-3 py-1.5 text-xs font-medium text-accent bg-accent-muted border border-accent-border rounded-lg hover:bg-accent/20 transition mt-3"
              >
                Run your first patch
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return <th className="text-left text-[11px] font-semibold text-fg-muted uppercase tracking-wide py-2.5 px-3">{children}</th>;
}
