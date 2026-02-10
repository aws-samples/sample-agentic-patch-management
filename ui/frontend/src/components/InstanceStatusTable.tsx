import { useMemo } from "react";
import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Box from "@cloudscape-design/components/box";
import type { InstanceState } from "../hooks/useWorkflowState";

interface Props {
  instances: InstanceState[];
}

const MAX_ROWS = 20;

// 7.5: Sort priority — failed first, then running, queued, success
const STATUS_ORDER: Record<InstanceState["status"], number> = {
  failed: 0,
  running: 1,
  queued: 2,
  success: 3,
};

const STATUS_TYPE: Record<InstanceState["status"], "error" | "in-progress" | "pending" | "success"> = {
  failed: "error",
  running: "in-progress",
  queued: "pending",
  success: "success",
};

export default function InstanceStatusTable({ instances }: Props) {
  const sorted = useMemo(
    () => [...instances].sort((a, b) => STATUS_ORDER[a.status] - STATUS_ORDER[b.status]),
    [instances]
  );
  const display = sorted.slice(0, MAX_ROWS);

  if (instances.length === 0) return null;

  return (
    <Table
      header={
        <Header
          counter={
            instances.length > MAX_ROWS
              ? `(showing ${MAX_ROWS} of ${instances.length})`
              : `(${instances.length})`
          }
        >
          Instances
        </Header>
      }
      columnDefinitions={[
        { id: "id", header: "Instance ID", cell: (item) => item.instanceId, width: 180 },
        { id: "team", header: "Team", cell: (item) => item.team || "—" },
        {
          id: "status",
          header: "Status",
          cell: (item) => (
            <StatusIndicator type={STATUS_TYPE[item.status]}>
              {item.status}
            </StatusIndicator>
          ),
        },
        {
          id: "elapsed",
          header: "Elapsed",
          cell: (item) => {
            if (item.elapsedMs == null) return "—";
            const s = Math.floor(item.elapsedMs / 1000);
            return s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
          },
        },
      ]}
      items={display}
      variant="embedded"
      empty={<Box textAlign="center" color="text-body-secondary">No instances</Box>}
    />
  );
}
