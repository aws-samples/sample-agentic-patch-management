import { useState } from "react";
import type { RunningOperation } from "../lib/api";

function elapsed(startedAt: string | null): string {
  if (!startedAt) return "—";
  const ms = Date.now() - new Date(startedAt).getTime();
  const mins = Math.floor(ms / 60_000);
  if (mins < 1) return "<1m";
  if (mins < 60) return `${mins}m`;
  return `${Math.floor(mins / 60)}h ${mins % 60}m`;
}

interface Props {
  operations: RunningOperation[];
}

export default function RunningOpsBadge({ operations }: Props) {
  const [expanded, setExpanded] = useState(false);

  if (operations.length === 0) return null;

  return (
    <div className="relative">
      <button
        onClick={() => setExpanded(!expanded)}
        className="inline-flex items-center gap-1.5 text-xs font-medium text-fg-muted hover:text-fg bg-surface-raised border border-edge rounded-lg px-2.5 py-1.5 transition"
      >
        <span className="relative flex h-2 w-2">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75" />
          <span className="relative inline-flex rounded-full h-2 w-2 bg-amber-500" />
        </span>
        {operations.length} running
      </button>

      {expanded && (
        <div className="absolute right-0 top-full mt-2 z-50 w-80 bg-surface-raised border border-edge rounded-xl shadow-xl p-3 space-y-2">
          <h4 className="text-xs font-semibold text-fg-muted uppercase tracking-wide mb-2">Running Operations</h4>
          {operations.map((op) => (
            <div key={op.execution_id} className="bg-surface/50 rounded-lg p-2.5 border border-edge/50">
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-semibold text-fg capitalize">{op.operation_type}</span>
                <span className="text-[10px] text-fg-muted font-mono">{elapsed(op.started_at)}</span>
              </div>
              <div className="text-[11px] text-fg-muted space-y-0.5">
                <div>Env: <span className="text-fg-secondary">{op.environment}</span></div>
                {op.instance_count > 0 && <div>Instances: <span className="text-fg-secondary">{op.instance_count}</span></div>}
                <div className="font-mono text-[10px] text-fg-faint truncate">{op.execution_id}</div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
