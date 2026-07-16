import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "../../lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 text-[11px] font-semibold px-2 py-0.5 rounded-full ring-1 ring-inset",
  {
    variants: {
      variant: {
        default: "bg-slate-400/10 text-fg-muted ring-slate-400/20",
        destructive: "bg-red-500/10 text-red-400 ring-red-500/20",
        warning: "bg-orange-500/10 text-orange-400 ring-orange-500/20",
        success: "bg-emerald-500/10 text-emerald-400 ring-emerald-500/20",
        info: "bg-sky-500/10 text-sky-400 ring-sky-500/20",
        outline: "bg-transparent text-fg-muted ring-slate-500/30",
      },
    },
    defaultVariants: { variant: "default" },
  }
);

type Variant = NonNullable<VariantProps<typeof badgeVariants>["variant"]>;

interface Props {
  variant?: Variant;
  children: React.ReactNode;
  className?: string;
  dot?: boolean;
}

export function Badge({ variant = "default", children, className, dot }: Props) {
  return (
    <span className={cn(badgeVariants({ variant }), className)}>
      {dot && <span className={`w-1.5 h-1.5 rounded-full ${DOT_COLORS[variant]}`} />}
      {children}
    </span>
  );
}

const DOT_COLORS: Record<Variant, string> = {
  default: "bg-slate-400",
  destructive: "bg-red-400",
  warning: "bg-orange-400",
  success: "bg-emerald-400",
  info: "bg-sky-400",
  outline: "bg-slate-500",
};

export function sevVariant(severity: string): Variant {
  switch (severity) {
    case "CRITICAL": return "destructive";
    case "HIGH": return "warning";
    case "MEDIUM": return "info";
    case "LOW": return "default";
    default: return "default";
  }
}

export function decisionVariant(decision: string): Variant {
  return decision === "EMERGENCY" ? "warning" : "info";
}

export function SlaBadge({ met }: { met: boolean | null | string }) {
  const isTrue = met === true || met === "True";
  const isFalse = met === false || met === "False";
  if (isTrue) return <Badge variant="success">Met</Badge>;
  if (isFalse) return <Badge variant="destructive">Breached</Badge>;
  return <span className="text-fg-faint text-xs">&mdash;</span>;
}
