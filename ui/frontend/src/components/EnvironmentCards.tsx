import Cards from "@cloudscape-design/components/cards";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Box from "@cloudscape-design/components/box";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
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
    <Cards
      loading={loading}
      loadingText="Loading environments..."
      header={<Header variant="h2">Environments</Header>}
      cardDefinition={{
        header: (item) => (
          <span
            style={{ cursor: onEnvClick ? "pointer" : "default" }}
            onClick={() => onEnvClick?.(item.environment)}
          >
            {item.environment.toUpperCase()}
          </span>
        ),
        sections: [
          {
            id: "status",
            content: (item) => (
              <StatusIndicator type={STATUS_MAP[item.status] ?? "stopped"}>
                {item.online}/{item.total} instances online
              </StatusIndicator>
            ),
          },
          {
            id: "vulns",
            content: (item) => {
              if (item.vuln_total === 0) {
                return (
                  <Box variant="small" color="text-status-success">
                    No vulnerabilities
                  </Box>
                );
              }
              const critical = item.vulns["CRITICAL"] ?? 0;
              const high = item.vulns["HIGH"] ?? 0;
              const other = item.vuln_total - critical - high;
              return (
                <SpaceBetween size="xxs" direction="horizontal">
                  {critical > 0 && (
                    <StatusIndicator type="error">{critical} CRITICAL</StatusIndicator>
                  )}
                  {high > 0 && (
                    <StatusIndicator type="warning">{high} HIGH</StatusIndicator>
                  )}
                  {other > 0 && (
                    <Box variant="small" color="text-body-secondary">
                      +{other} other
                    </Box>
                  )}
                </SpaceBetween>
              );
            },
          },
        ],
      }}
      items={items}
      cardsPerRow={[{ cards: 3 }]}
    />
  );
}
