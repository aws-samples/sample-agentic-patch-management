/** shadcn-inspired Badge — consistent severity/status indicators across the app.
 *  Danger/warning variants use saturated colors for urgency;
 *  success/info stay muted so problems naturally draw the eye. */

const VARIANTS = {
  default: "bg-gray-100 text-gray-800 ring-gray-200",
  destructive: "bg-red-100 text-red-800 ring-red-300",
  warning: "bg-amber-100 text-amber-800 ring-amber-300",
  success: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  info: "bg-sky-50 text-sky-700 ring-sky-200",
  outline: "bg-transparent text-gray-700 ring-gray-300",
} as const;

type Variant = keyof typeof VARIANTS;

interface Props {
  variant?: Variant;
  children: React.ReactNode;
  className?: string;
  dot?: boolean;
}

export function Badge({ variant = "default", children, className = "", dot }: Props) {
  return (
    <span className={`inline-flex items-center gap-1 text-[11px] font-semibold px-2 py-0.5 rounded-full ring-1 ring-inset ${VARIANTS[variant]} ${className}`}>
      {dot && <span className={`w-1.5 h-1.5 rounded-full ${DOT_COLORS[variant]}`} />}
      {children}
    </span>
  );
}

const DOT_COLORS: Record<Variant, string> = {
  default: "bg-gray-500",
  destructive: "bg-red-600",
  warning: "bg-amber-600",
  success: "bg-emerald-500",
  info: "bg-sky-500",
  outline: "bg-gray-500",
};

/** Map severity strings to badge variants */
export function sevVariant(severity: string): Variant {
  switch (severity) {
    case "CRITICAL": return "destructive";
    case "HIGH": return "warning";
    case "MEDIUM": return "info";
    case "LOW": return "default";
    default: return "default";
  }
}

/** Map decision strings to badge variants */
export function decisionVariant(decision: string): Variant {
  return decision === "EMERGENCY" ? "warning" : "info";
}

/** SLA badge — breached state is intentionally bold */
export function SlaBadge({ met }: { met: boolean | null | string }) {
  const isTrue = met === true || met === "True";
  const isFalse = met === false || met === "False";
  if (isTrue) return <Badge variant="success">✓ Met</Badge>;
  if (isFalse) return <Badge variant="destructive" className="animate-pulse">✗ Breached</Badge>;
  return <span className="text-gray-400 text-xs">—</span>;
}
