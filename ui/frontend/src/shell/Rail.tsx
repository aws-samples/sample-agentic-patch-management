import { refreshText } from "../lib/view";
import type { Pane } from "../views/pane";

interface Props {
  current: Pane;
  onOpen: (pane: Pane) => void;
  waiting: number;
  running: number;
  findings: number | null;
  reports: number | null;
  refreshedAt: Date | null;
  /** The last refresh failed, so what's shown is from refreshedAt. */
  refreshFailed: boolean;
  loading: boolean;
  onRefresh: () => void;
}

/** What the detail pane can show. Changes counts in violet: they modify instances. */
export default function Rail({ current, onOpen, waiting, running, findings, reports, refreshedAt, refreshFailed, loading, onRefresh }: Props) {
  const item = (kind: "overview" | "changes" | "findings" | "reports", label: string, count?: React.ReactNode) => {
    const on = current.kind === kind || (kind === "changes" && (current.kind === "change" || current.kind === "run"));
    return (
      <button type="button" aria-current={on ? "page" : undefined} onClick={() => onOpen({ kind } as Pane)}>
        <span>{label}</span>{count}
      </button>
    );
  };
  return (
    <nav className="rail" aria-label="Show in the detail pane">
      {item("overview", "Overview")}
      {item("changes", "Changes", waiting ? <span className="n change">{waiting}<span className="wide"> waiting</span></span>
        : running ? <span className="n change">{running}<span className="wide"> running</span></span> : null)}
      {item("findings", "Findings", findings != null ? <span className="n">{findings}</span> : null)}
      {item("reports", "Reports", reports != null ? <span className="n">{reports}</span> : null)}
      <div className="foot">
        <span className={refreshFailed && !loading ? "watch" : undefined} role={refreshFailed ? "status" : undefined}>
          {refreshText(loading, refreshedAt, refreshFailed)}
        </span>
        <div><button type="button" onClick={onRefresh} disabled={loading} aria-label="Refresh the dashboard">Refresh</button></div>
      </div>
    </nav>
  );
}
