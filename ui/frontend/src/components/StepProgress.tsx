import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Spinner from "@cloudscape-design/components/spinner";
import type { StepState } from "../hooks/useWorkflowState";

interface Props {
  steps: StepState[];
  envName: string;
}

const STATUS_TYPE: Record<StepState["status"], "success" | "in-progress" | "pending" | "error"> = {
  complete: "success",
  active: "in-progress",
  pending: "pending",
  failed: "error",
};

/** Extract a short data summary for display next to each step. */
function stepDetail(step: StepState): string {
  const d = step.data;
  switch (step.id) {
    case "discovery": {
      const ids = Array.isArray(d.instance_ids) ? d.instance_ids as string[] : [];
      if (ids.length) return `${ids.length} instances`;
      return "";
    }
    case "window_check":
      return d.window_name ? String(d.window_name) : "";
    case "sla_decision":
      return [d.decision, d.sla_hours != null && `${d.sla_hours}h SLA`, d.framework]
        .filter(Boolean)
        .join(" · ");
    case "patch_execute":
      return d.command_id ? `cmd: ${String(d.command_id).slice(0, 16)}` : "";
    case "health_check": {
      const total = d.total_instances;
      const unhealthy = d.unhealthy_instances;
      if (total != null) return `${Number(total) - Number(unhealthy ?? 0)}/${total} healthy`;
      return "";
    }
    case "alarm_check": {
      const alarms = d.total_alarms_firing;
      if (alarms != null) return Number(alarms) === 0 ? "No alarms" : `${alarms} alarms firing`;
      return "";
    }
    case "report":
      return d.report_id ? `Report: ${String(d.report_id).slice(0, 12)}` : "";
    default:
      return "";
  }
}

export default function StepProgress({ steps, envName }: Props) {
  return (
    <SpaceBetween size="xxs">
      <Box fontWeight="bold" fontSize="body-s" color="text-body-secondary">
        Steps — {envName.toUpperCase()}
      </Box>
      {steps.map((step) => {
        const isSla = step.id === "sla_decision";
        const detail = stepDetail(step);
        return (
          <div
            key={step.id}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "8px",
              padding: "4px 8px",
              borderRadius: "4px",
              borderLeft: isSla ? "3px solid #0972d3" : "3px solid transparent",
              background: isSla ? "#f0f8ff" : "transparent",
            }}
          >
            {/* 6.5: Active spinner */}
            {step.status === "active" ? (
              <Spinner size="normal" />
            ) : (
              <StatusIndicator type={STATUS_TYPE[step.status]}>
                {step.label}
              </StatusIndicator>
            )}
            {step.status === "active" && (
              <Box fontWeight="bold" fontSize="body-s">
                {step.label}
              </Box>
            )}
            {/* 6.3: Key data point */}
            {detail && (
              <Box color="text-body-secondary" fontSize="body-s">
                — {detail}
              </Box>
            )}
          </div>
        );
      })}
    </SpaceBetween>
  );
}
