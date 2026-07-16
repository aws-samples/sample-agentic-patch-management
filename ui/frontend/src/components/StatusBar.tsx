interface Props {
  lastRefreshed: Date | null;
  runningOpsCount: number;
  slaRate: number | null;
  onRunningOpsClick?: () => void;
}

export default function StatusBar({ lastRefreshed, runningOpsCount, slaRate, onRunningOpsClick }: Props) {
  const timeStr = lastRefreshed
    ? lastRefreshed.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : "\u2014";

  const slaColor =
    slaRate === null
      ? "text-fg-muted"
      : slaRate >= 95
        ? "text-emerald-400"
        : slaRate >= 80
          ? "text-amber-400"
          : "text-red-400";

  const divider = <span className="text-fg-faint select-none">&middot;</span>;

  return (
    <div className="flex items-center gap-3 px-3 py-1.5 bg-surface-card border border-edge rounded-lg text-[11px] text-fg-muted font-mono">
      {/* Last refresh */}
      <span>Last refresh: {timeStr}</span>

      {divider}

      {/* Running operations */}
      {runningOpsCount > 0 ? (
        <button
          onClick={onRunningOpsClick}
          className="flex items-center gap-1.5 hover:text-fg-secondary transition"
        >
          <span className="relative flex h-2 w-2">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-teal-400 opacity-75" />
            <span className="relative inline-flex rounded-full h-2 w-2 bg-teal-500" />
          </span>
          {runningOpsCount} operation{runningOpsCount !== 1 ? "s" : ""} running
        </button>
      ) : (
        <span>0 operations running</span>
      )}

      {divider}

      {/* SLA rate */}
      <span>
        SLA: <span className={slaColor}>{slaRate !== null ? `${slaRate}%` : "\u2014"}</span>
        {slaRate !== null && <span className="text-fg-faint ml-1">(30d)</span>}
      </span>
    </div>
  );
}
