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
}

export default function EnvironmentCards({ items, loading, onEnvClick }: Props) {
  if (loading && items.length === 0) {
    return <CardsSkeleton count={4} />;
  }
  if (items.length === 0) {
    return (
      <div className="text-center py-12">
        <div className="text-3xl mb-2">🌐</div>
        <p className="text-sm font-medium text-gray-700">No environments found</p>
        <p className="text-xs text-gray-500 mt-1">Use the chat to scan your fleet</p>
      </div>
    );
  }

  return (
    <div>
      <h3 className="text-base font-semibold text-gray-900 mb-3">Environments</h3>
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        {items.map((item) => {
          const variant = STATUS_VARIANT[item.status] ?? "default";
          const healthPct = item.total > 0 ? Math.round((item.online / item.total) * 100) : 0;
          return (
            <Card
              key={item.environment}
              hover
              onClick={() => onEnvClick?.(item.environment)}
              className="p-4 group hover:border-blue-200"
            >
              {/* Header */}
              <div className="flex items-center justify-between mb-3">
                <span className="font-bold text-blue-600 group-hover:text-blue-700 transition text-sm">
                  {item.environment.toUpperCase()}
                </span>
                <Badge variant={variant} dot>{item.status}</Badge>
              </div>

              {/* Instance health bar */}
              <div className="mb-3">
                <div className="flex justify-between text-[11px] text-gray-500 mb-1">
                  <span>Instances</span>
                  <span className="font-medium text-gray-700">{item.online}/{item.total} online</span>
                </div>
                <div className="w-full bg-gray-100 rounded-full h-1.5">
                  <div
                    className={`h-1.5 rounded-full transition-all duration-500 ${healthPct === 100 ? "bg-emerald-500" : healthPct >= 50 ? "bg-amber-500" : "bg-red-500"}`}
                    style={{ width: `${healthPct}%` }}
                  />
                </div>
              </div>

              {/* Vuln summary */}
              <div className="flex gap-1.5">
                {(item.vulns["CRITICAL"] ?? 0) > 0 && <Badge variant={sevVariant("CRITICAL")}>{item.vulns["CRITICAL"]} Critical</Badge>}
                {(item.vulns["HIGH"] ?? 0) > 0 && <Badge variant={sevVariant("HIGH")}>{item.vulns["HIGH"]} High</Badge>}
                {(item.vulns["MEDIUM"] ?? 0) > 0 && <Badge variant={sevVariant("MEDIUM")}>{item.vulns["MEDIUM"]} Medium</Badge>}
                {item.vuln_total === 0 && <Badge variant="success">✓ No vulnerabilities</Badge>}
              </div>
            </Card>
          );
        })}
      </div>
    </div>
  );
}


