import { useEffect, useState } from "react";
import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Box from "@cloudscape-design/components/box";
import type { ActivityItem } from "../lib/api";
import { api } from "../lib/api";

interface Props {
  onRowClick?: (cveId: string) => void;
}

export default function ActivityTable({ onRowClick }: Props) {
  const [items, setItems] = useState<ActivityItem[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.activity().then((d) => { setItems(d.activities); setLoading(false); })
      .catch(() => setLoading(false));
  }, []);

  return (
    <Table
      loading={loading}
      loadingText="Loading activity..."
      header={<Header variant="h2">Recent Patching Activity</Header>}
      columnDefinitions={[
        {
          id: "time",
          header: "Time",
          cell: (item) => new Date(item.timestamp).toLocaleString(),
          sortingField: "timestamp",
        },
        {
          id: "cve",
          header: "CVE",
          cell: (item) => (
            <span
              style={{ cursor: onRowClick ? "pointer" : "default", color: "#0972d3" }}
              onClick={() => onRowClick?.(item.cve_id)}
            >
              {item.cve_id}
            </span>
          ),
        },
        {
          id: "env",
          header: "Env",
          cell: (item) => item.environment,
        },
        {
          id: "decision",
          header: "Type",
          cell: (item) => (
            <StatusIndicator type={item.decision === "EMERGENCY" ? "warning" : "info"}>
              {item.decision}
            </StatusIndicator>
          ),
        },
        {
          id: "sla",
          header: "SLA",
          cell: (item) => (
            item.sla_met === "True"
              ? <StatusIndicator type="success">Met</StatusIndicator>
              : item.sla_met === "False"
              ? <StatusIndicator type="error">Breached</StatusIndicator>
              : <Box color="text-status-inactive">—</Box>
          ),
        },
      ]}
      items={items}
      variant="embedded"
      empty={<Box textAlign="center" color="text-body-secondary">No patching activity yet</Box>}
    />
  );
}
