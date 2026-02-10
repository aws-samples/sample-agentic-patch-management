import Box from "@cloudscape-design/components/box";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import type { EnvState } from "../hooks/useWorkflowState";

interface Props {
  environments: EnvState[];
  currentEnvIndex: number;
}

const STATUS_ICON: Record<EnvState["status"], string> = {
  complete: "✅",
  active: "🔄",
  pending: "⏳",
  failed: "❌",
  skipped: "⏭️",
};

const STATUS_TYPE: Record<EnvState["status"], "success" | "in-progress" | "pending" | "error" | "stopped"> = {
  complete: "success",
  active: "in-progress",
  pending: "pending",
  failed: "error",
  skipped: "stopped",
};

function formatElapsed(env: EnvState): string {
  if (!env.startTime) return "";
  const end = env.endTime ?? Date.now();
  const sec = Math.floor((end - env.startTime) / 1000);
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function successRatio(env: EnvState): string {
  if (env.instances.length === 0) return "";
  const ok = env.instances.filter((i) => i.status === "success").length;
  return `${ok}/${env.instances.length} ✓`;
}

export default function EnvPipeline({ environments, currentEnvIndex }: Props) {
  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        gap: "8px",
      }}
    >
      {environments.map((env, idx) => {
        const isActive = idx === currentEnvIndex && env.status === "active";
        return (
          <div key={env.name} style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            {/* 5.4: Arrow between nodes */}
            {idx > 0 && (
              <Box color="text-body-secondary" fontSize="heading-m">
                →
              </Box>
            )}
            {/* 5.3 & 5.5: Environment node */}
            <div
              style={{
                padding: "8px 14px",
                borderRadius: "8px",
                border: isActive ? "2px solid #0972d3" : "1px solid #d1d5db",
                background: isActive ? "#f0f8ff" : "transparent",
                textAlign: "center",
                minWidth: "90px",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "6px" }}>
                <span>{STATUS_ICON[env.status]}</span>
                <Box fontWeight="bold" fontSize="body-s">
                  {env.name.toUpperCase()}
                </Box>
              </div>
              <StatusIndicator type={STATUS_TYPE[env.status]}>
                {env.status}
              </StatusIndicator>
              {formatElapsed(env) && (
                <Box color="text-body-secondary" fontSize="body-s">
                  {formatElapsed(env)}
                </Box>
              )}
              {successRatio(env) && (
                <Box color="text-body-secondary" fontSize="body-s">
                  {successRatio(env)}
                </Box>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
