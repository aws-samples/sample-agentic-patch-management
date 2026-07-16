import { Badge, sevVariant } from "./ui/Badge";
import { Card } from "./ui/Card";
import { CardsSkeleton } from "./ui/Skeleton";
import type { EnvironmentStatus } from "../lib/api";

const STATUS_VARIANT: Record<string, "success" | "warning" | "destructive" | "default"> = {
  healthy: "success",
  warning: "warning",
  error: "destructive",
  inactive: "default",
};

interface Props {
  items: EnvironmentStatus[];
  loading: boolean;
  onEnvClick?: (env: string) => void;
  onQuickAction?: (text: string) => void;
}

export default function EnvironmentCards({ items, loading, onEnvClick, onQuickAction }: Props) {
  if (loading && items.length === 0) {
    return <CardsSkeleton count={4} />;
  }
  if (items.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-center">
        <svg className="w-8 h-8 text-fg-faint mb-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M12 21a9.004 9.004 0 008.716-6.747M12 21a9.004 9.004 0 01-8.716-6.747M12 21c2.485 0 4.5-4.03 4.5-9S14.485 3 12 3m0 18c-2.485 0-4.5-4.03-4.5-9S9.515 3 12 3m0 0a8.997 8.997 0 017.843 4.582M12 3a8.997 8.997 0 00-7.843 4.582m15.686 0A11.953 11.953 0 0112 10.5c-2.998 0-5.74-1.1-7.843-2.918m15.686 0A8.959 8.959 0 0121 12c0 .778-.099 1.533-.284 2.253m0 0A17.919 17.919 0 0112 16.5c-3.162 0-6.133-.815-8.716-2.247m0 0A9.015 9.015 0 013 12c0-1.605.42-3.113 1.157-4.418" /></svg>
        <p className="text-sm font-medium text-fg-secondary">No environments found</p>
        <p className="text-xs text-fg-muted mt-1 max-w-sm">
          SSM Explorer may take up to 6 hours to populate after AWS Config is enabled in spoke accounts.
          The chat agent can discover instances immediately via direct queries.
        </p>
      </div>
    );
  }

  return (
    <div>
      <h3 className="text-base font-semibold text-fg mb-3">Environments</h3>
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        {items.map((item, idx) => {
          const variant = STATUS_VARIANT[item.status] ?? "default";
          const healthPct = item.total > 0 ? Math.round((item.online / item.total) * 100) : 0;
          return (
            <Card
              key={item.environment}
              hover
              onClick={() => onEnvClick?.(item.environment)}
              className={`p-4 group hover:border-accent-border animate-slide-up stagger-${Math.min(idx + 1, 5)}`}
            >
              {/* Header */}
              <div className="flex items-center justify-between mb-3">
                <span className="font-bold text-accent group-hover:text-accent-hover transition text-sm tracking-wide">
                  {item.environment.toUpperCase()}
                </span>
                <Badge variant={variant} dot>{item.status}</Badge>
              </div>

              {/* Instance health bar */}
              <div className="mb-3">
                <div className="flex justify-between text-[11px] text-fg-muted mb-1">
                  <span>Instances</span>
                  <span className="font-medium text-fg-secondary font-mono">{item.online}/{item.total} online</span>
                </div>
                <div className="w-full bg-hover rounded-full h-1.5" role="meter" aria-label={`Instance health: ${healthPct}%`} aria-valuenow={healthPct} aria-valuemin={0} aria-valuemax={100}>
                  <div
                    className={`h-1.5 rounded-full transition-all duration-500 ${healthPct === 100 ? "bg-emerald-400" : healthPct >= 50 ? "bg-amber-400" : "bg-red-400"}`}
                    style={{ width: `${healthPct}%` }}
                  />
                </div>
              </div>

              {/* Patch compliance bar — sourced from SSM Patch Manager */}
              {item.patch_compliance && item.patch_compliance.compliance_pct !== null && (
                <div className="mb-3">
                  <div className="flex justify-between text-[11px] text-fg-muted mb-1">
                    <span>
                      Patch Compliance
                      <span className="text-[10px] text-fg-faint font-mono ml-1.5">· SSM Patch Manager</span>
                    </span>
                    <span className="font-medium text-fg-secondary font-mono">
                      {item.patch_compliance.compliance_pct}% ({item.patch_compliance.compliant_instances}/{item.patch_compliance.scanned_instances})
                    </span>
                  </div>
                  <div className="w-full bg-hover rounded-full h-1.5">
                    <div
                      className={`h-1.5 rounded-full transition-all duration-500 ${item.patch_compliance.compliance_pct === 100 ? "bg-emerald-400" : item.patch_compliance.compliance_pct >= 80 ? "bg-amber-400" : "bg-red-400"}`}
                      style={{ width: `${item.patch_compliance.compliance_pct}%` }}
                    />
                  </div>
                  {item.patch_compliance.missing_patches > 0 && (
                    <div className="text-[10px] text-fg-faint mt-0.5 font-mono">
                      {item.patch_compliance.missing_patches} missing &middot; {item.patch_compliance.installed_patches} installed
                    </div>
                  )}
                </div>
              )}

              {/* Vuln summary — sourced from Amazon Inspector */}
              <div className="mb-3">
                <div className="text-[11px] text-fg-muted mb-1">
                  Vulnerabilities
                  <span className="text-[10px] text-fg-faint font-mono ml-1.5">· Amazon Inspector</span>
                </div>
                <div className="flex gap-1.5 flex-wrap">
                  {(item.vulns["CRITICAL"] ?? 0) > 0 && <Badge variant={sevVariant("CRITICAL")}>{item.vulns["CRITICAL"]} Critical</Badge>}
                  {(item.vulns["HIGH"] ?? 0) > 0 && <Badge variant={sevVariant("HIGH")}>{item.vulns["HIGH"]} High</Badge>}
                  {(item.vulns["MEDIUM"] ?? 0) > 0 && <Badge variant={sevVariant("MEDIUM")}>{item.vulns["MEDIUM"]} Medium</Badge>}
                  {item.vuln_total === 0 && <Badge variant="success">No vulnerabilities</Badge>}
                </div>
              </div>

              {/* Quick actions */}
              {onQuickAction && (
                <div className="flex gap-1.5 pt-2 border-t border-edge">
                  <button
                    onClick={(e) => { e.stopPropagation(); onQuickAction(`Preview patches for ${item.environment}`); }}
                    className="text-[10px] font-medium text-fg-muted hover:text-accent bg-input hover:bg-accent-muted border border-edge rounded-md px-2 py-1 transition"
                  >
                    Scan
                  </button>
                  {item.vuln_total > 0 && (
                    <button
                      onClick={(e) => { e.stopPropagation(); onQuickAction(`Patch all critical CVEs in ${item.environment}`); }}
                      className="text-[10px] font-medium text-fg-muted hover:text-orange-400 bg-input hover:bg-orange-400/[0.06] border border-edge rounded-md px-2 py-1 transition"
                    >
                      Patch critical
                    </button>
                  )}
                  <button
                    onClick={(e) => { e.stopPropagation(); onQuickAction(`Generate compliance report for ${item.environment}`); }}
                    className="text-[10px] font-medium text-fg-muted hover:text-accent bg-input hover:bg-accent-muted border border-edge rounded-md px-2 py-1 transition"
                  >
                    Report
                  </button>
                </div>
              )}
            </Card>
          );
        })}
      </div>
    </div>
  );
}
