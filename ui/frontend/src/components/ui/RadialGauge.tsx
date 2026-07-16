interface Props {
  rate: number | null; // 0-100 or null for no data
  size?: number; // default 44
  strokeWidth?: number; // default 4
  fillColor?: string; // direct color value
  trackColor?: string; // direct color value for track
  className?: string;
}

export function RadialGauge({
  rate,
  size = 44,
  strokeWidth = 4,
  fillColor = "#34d399",
  trackColor = "var(--color-edge)",
  className = "",
}: Props) {
  const r = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * r;
  const offset = rate !== null ? circumference * (1 - rate / 100) : circumference;

  return (
    <div className={`relative shrink-0 ${className}`} style={{ width: size, height: size }}>
      <svg viewBox={`0 0 ${size} ${size}`} className="w-full h-full -rotate-90">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={trackColor}
          strokeWidth={strokeWidth}
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={fillColor}
          strokeWidth={strokeWidth}
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          className="transition-all duration-700"
        />
      </svg>
      <div className="absolute inset-0 flex items-center justify-center">
        <span className="text-[10px] font-bold font-mono text-fg">
          {rate !== null ? Math.round(rate) : "\u2014"}
        </span>
      </div>
    </div>
  );
}
