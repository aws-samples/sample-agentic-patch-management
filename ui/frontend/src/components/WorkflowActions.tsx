import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import type { WorkflowState } from "../hooks/useWorkflowState";

interface Props {
  workflowState: WorkflowState;
  isStreaming: boolean;
  onSend: (text: string) => void;
}

export default function WorkflowActions({ workflowState, isStreaming, onSend }: Props) {
  const { environments, currentEnvIndex, active } = workflowState;
  const currentEnv = environments[currentEnvIndex];
  const nextEnv = environments[currentEnvIndex + 1];

  // 8.2 & 8.3: Show proceed buttons when current env is complete and next is pending
  const showProceed =
    currentEnv?.status === "complete" && nextEnv?.status === "pending";

  if (!active) return null;

  return (
    <SpaceBetween direction="horizontal" size="s">
      {/* 8.2: Proceed */}
      {showProceed && (
        <Button
          variant="primary"
          disabled={isStreaming}
          onClick={() => onSend("proceed")}
        >
          Proceed
        </Button>
      )}

      {/* 8.3: Verify & Proceed */}
      {showProceed && nextEnv && (
        <Button
          variant="normal"
          disabled={isStreaming}
          onClick={() => onSend(`verify and proceed to ${nextEnv.name}`)}
        >
          Verify &amp; Proceed
        </Button>
      )}

      {/* 8.4: Abort — always visible when active */}
      <Button
        variant="link"
        disabled={isStreaming}
        onClick={() => onSend("stop patching, do not proceed to next environment")}
      >
        <span style={{ color: "#d91515" }}>Abort</span>
      </Button>
    </SpaceBetween>
  );
}
