import { useState, useEffect } from "react";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import type { WorkflowState } from "../hooks/useWorkflowState";

interface Props {
  workflowState: WorkflowState;
  estimatedRemainingMs: number | null;
}

function severityType(sev: string | null): "error" | "warning" | "info" {
  if (!sev) return "info";
  const upper = sev.toUpperCase();
  if (upper === "CRITICAL" || upper === "HIGH") return "error";
  if (upper === "MEDIUM") return "warning";
  return "info";
}

function formatElapsed(ms: number): string {
  const totalSec = Math.floor(ms / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function formatEta(ms: number | null): string {
  if (ms === null) return "Estimating...";
  const minutes = Math.max(1, Math.round(ms / 60000));
  return `~${minutes}m remaining`;
}

export default function WorkflowHeader({ workflowState, estimatedRemainingMs }: Props) {
  // 4.3: Live elapsed time counter
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    if (!workflowState.active || !workflowState.startTime) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [workflowState.active, workflowState.startTime]);

  const elapsed = workflowState.startTime ? now - workflowState.startTime : 0;

  // 4.5: Progress counts
  const completedEnvs = workflowState.environments.filter(
    (e) => e.status === "complete" || e.status === "skipped"
  ).length;
  const totalEnvs = workflowState.environments.length;
  const totalInstances = workflowState.environments.reduce((s, e) => s + e.instances.length, 0);
  const doneInstances = workflowState.environments.reduce(
    (s, e) => s + e.instances.filter((i) => i.status === "success" || i.status === "failed").length,
    0
  );

  return (
    <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "16px" }}>
      {/* 4.2: CVE ID + severity badge */}
      {workflowState.cveId && (
        <Box fontWeight="bold" fontSize="heading-s">
          {workflowState.cveId}
        </Box>
      )}
      {workflowState.severity && (
        <StatusIndicator type={severityType(workflowState.severity)}>
          {workflowState.severity.toUpperCase()}
        </StatusIndicator>
      )}

      <SpaceBetween direction="horizontal" size="m">
        {/* 4.3: Elapsed time */}
        <Box color="text-body-secondary" fontSize="body-s">
          ⏱ {formatElapsed(elapsed)}
        </Box>

        {/* 4.4: ETA */}
        {workflowState.active && (
          <Box color="text-body-secondary" fontSize="body-s">
            {formatEta(estimatedRemainingMs)}
          </Box>
        )}

        {/* 4.5: Progress */}
        {totalEnvs > 0 && (
          <Box color="text-body-secondary" fontSize="body-s">
            Environment {completedEnvs}/{totalEnvs}
            {totalInstances > 0 && ` · ${doneInstances}/${totalInstances} instances`}
          </Box>
        )}
      </SpaceBetween>
    </div>
  );
}
