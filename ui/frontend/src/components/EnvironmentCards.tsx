import { useEffect, useState } from "react";
import Cards from "@cloudscape-design/components/cards";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Box from "@cloudscape-design/components/box";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import type { EnvironmentStatus } from "../lib/api";
import { api } from "../lib/api";

const STATUS_MAP: Record<string, "success" | "warning" | "error" | "stopped"> = {
  healthy: "success",
  warning: "warning",
  error: "error",
  inactive: "stopped",
};

interface Props {
  onEnvClick?: (env: string) => void;
}

export default function EnvironmentCards({ onEnvClick }: Props) {
  const [items, setItems] = useState<EnvironmentStatus[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.environments().then((d) => { setItems(d.environments); setLoading(false); })
      .catch(() => setLoading(false));
  }, []);

  return (
    <Cards
      loading={loading}
      loadingText="Loading environments..."
      header={<Header variant="h2">Environment Status</Header>}
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
                {item.online}/{item.total} online
              </StatusIndicator>
            ),
          },
          {
            id: "details",
            content: (item) => (
              <SpaceBetween size="xxs">
                <Box variant="small">
                  Instances: {item.total} &nbsp;|&nbsp; Offline: {item.offline}
                </Box>
              </SpaceBetween>
            ),
          },
        ],
      }}
      items={items}
      cardsPerRow={[{ cards: 3 }]}
    />
  );
}
