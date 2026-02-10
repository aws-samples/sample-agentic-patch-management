import Table from "@cloudscape-design/components/table";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Box from "@cloudscape-design/components/box";
import Header from "@cloudscape-design/components/header";
import type { EnvironmentStatus } from "../lib/api";

const STATUS_MAP: Record<string, "success" | "warning" | "error" | "stopped"> = {
  healthy: "success",
  warning: "warning",
  error: "error",
  inactive: "stopped",
};

interface Props {
  items: EnvironmentStatus[];
  loading: boolean;
  onEnvClick?: (env: string) => void;
}

export default function EnvironmentCards({ items, loading, onEnvClick }: Props) {
  return (
    <Table
      loading={loading}
      loadingText="Loading environments..."
      header={<Header variant="h2">Environments</Header>}
      columnDefinitions={[
        {
          id: "env",
          header: "Environment",
          cell: (item) => (
            <span
              style={{ cursor: onEnvClick ? "pointer" : "default", color: "#0972d3", fontWeight: 600 }}
              onClick={() => onEnvClick?.(item.environment)}
            >
              {item.environment.toUpperCase()}
            </span>
          ),
        },
        {
          id: "status",
          header: "Status",
          cell: (item) => (
            <StatusIndicator type={STATUS_MAP[item.status] ?? "stopped"}>
              {item.status}
            </StatusIndicator>
          ),
        },
        {
          id: "instances",
          header: "Instances",
          cell: (item) => `${item.online} / ${item.total} online`,
        },
        {
          id: "critical",
          header: "Critical",
          cell: (item) => {
            const count = item.vulns["CRITICAL"] ?? 0;
            return count > 0
              ? <StatusIndicator type="error">{count}</StatusIndicator>
              : <Box color="text-status-inactive">0</Box>;
          },
        },
        {
          id: "high",
          header: "High",
          cell: (item) => {
            const count = item.vulns["HIGH"] ?? 0;
            return count > 0
              ? <StatusIndicator type="warning">{count}</StatusIndicator>
              : <Box color="text-status-inactive">0</Box>;
          },
        },
        {
          id: "medium",
          header: "Medium",
          cell: (item) => {
            const count = item.vulns["MEDIUM"] ?? 0;
            return count > 0
              ? <Box>{count}</Box>
              : <Box color="text-status-inactive">0</Box>;
          },
        },
        {
          id: "total_vulns",
          header: "Total Vulnerabilities",
          cell: (item) => item.vuln_total > 0
            ? <Box fontWeight="bold">{item.vuln_total}</Box>
            : <Box color="text-status-success">None</Box>,
        },
      ]}
      items={items}
      variant="container"
      empty={<Box textAlign="center" color="text-body-secondary">No environments found</Box>}
    />
  );
}
