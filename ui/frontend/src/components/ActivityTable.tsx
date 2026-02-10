import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Box from "@cloudscape-design/components/box";
import type { ActivityItem } from "../lib/api";

interface Props {
  items: ActivityItem[];
  loading: boolean;
  onRowClick?: (cveId: string) => void;
}

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(iso).toLocaleDateString();
}

export default function ActivityTable({ items, loading, onRowClick }: Props) {
  // Show only 5 most recent
  const recent = items.slice(0, 5);

  return (
    <Table
      loading={loading}
      loadingText="Loading activity..."
      header={
        <Header variant="h2" counter={`(${items.length} total)`}>
          Recent Activity
        </Header>
      }
      columnDefinitions={[
        {
          id: "time",
          header: "When",
          cell: (item) => (
            <Box variant="small">
              {relativeTime(item.timestamp)}
            </Box>
          ),
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
          cell: (item) =>
            item.sla_met === "True" ? (
              <StatusIndicator type="success">Met</StatusIndicator>
            ) : item.sla_met === "False" ? (
              <StatusIndicator type="error">Breached</StatusIndicator>
            ) : (
              <Box color="text-status-inactive">—</Box>
            ),
        },
      ]}
      items={recent}
      variant="container"
      empty={<Box textAlign="center" color="text-body-secondary">No patching activity yet</Box>}
    />
  );
}
