import { useState, useEffect } from "react";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import SpaceBetween from "@cloudscape-design/components/space-between";
import type { WorkflowState } from "../hooks/useWorkflowState";
import WorkflowHeader from "./WorkflowHeader";
import EnvPipeline from "./EnvPipeline";
import StepProgress from "./StepProgress";
import InstanceStatusTable from "./InstanceStatusTable";
import WorkflowActions from "./WorkflowActions";

interface Props {
  workflowState: WorkflowState;
  estimatedRemainingMs: number | null;
  isStreaming: boolean;
  onSend: (text: string) => void;
}

function summaryLine(state: WorkflowState): string {
  const cve = state.cveId ?? "CVE";
  const envCount = state.environments.length;
  const totalInst = state.environments.reduce((s, e) => s + e.instances.length, 0);
  const okInst = state.environments.reduce(
    (s, e) => s + e.instances.filter((i) => i.status === "success").length,
    0
  );
  let elapsed = "";
  if (state.startTime && state.completedAt) {
    const sec = Math.floor((state.completedAt - state.startTime) / 1000);
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    elapsed = m > 0 ? `${m}m ${s}s` : `${s}s`;
  }
  return `✅ ${cve} patched across ${envCount} environments${elapsed ? ` in ${elapsed}` : ""} — ${okInst}/${totalInst} instances`;
}

export default function WorkflowTracker({ workflowState, estimatedRemainingMs, isStreaming, onSend }: Props) {
  // 9.5: Don't render when inactive and never completed
  if (!workflowState.active && workflowState.completedAt === null) return null;

  const isCompleted = workflowState.completedAt !== null;
  const activeEnv = workflowState.environments[workflowState.currentEnvIndex];

  // 9.3 & 9.4: Expanded when active, collapsed when completed
  const [expanded, setExpanded] = useState(!isCompleted);

  // Auto-collapse when workflow completes
  useEffect(() => {
    if (isCompleted) setExpanded(false);
    else setExpanded(true);
  }, [isCompleted]);

  return (
    <ExpandableSection
      variant="container"
      expanded={expanded}
      onChange={({ detail }) => setExpanded(detail.expanded)}
      headerText={
        isCompleted
          ? summaryLine(workflowState)
          : "🔄 Workflow Tracker"
      }
    >
      <SpaceBetween size="m">
        <WorkflowHeader
          workflowState={workflowState}
          estimatedRemainingMs={estimatedRemainingMs}
        />
        <EnvPipeline
          environments={workflowState.environments}
          currentEnvIndex={workflowState.currentEnvIndex}
        />
        {activeEnv && activeEnv.status === "active" && (
          <>
            <StepProgress steps={activeEnv.steps} envName={activeEnv.name} />
            <InstanceStatusTable instances={activeEnv.instances} />
          </>
        )}
        {/* 9.6: Pass onSend and isStreaming through */}
        <WorkflowActions
          workflowState={workflowState}
          isStreaming={isStreaming}
          onSend={onSend}
        />
      </SpaceBetween>
    </ExpandableSection>
  );
}
