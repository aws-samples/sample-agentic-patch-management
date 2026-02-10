import { useEffect, useState } from "react";
import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Box from "@cloudscape-design/components/box";
import type { VulnerabilityFinding } from "../lib/api";
import { api } from "../lib/api";

const SEV_TYPE: Record<string, "error" | "warning" | "info" | "stopped"> = {
  CRITICAL: "error",
  HIGH: "warning",
  MEDIUM: "info",
  LOW: "stopped",
};

interface Props {
  onCveClick?: (cveId: string, env: string) => void;
}

export default function VulnTable({ onCveClick }: Props) {
  const [items, setItems] = useState<VulnerabilityFinding[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.vulnerabilities().then((d) => { setItems(d.findings); setLoading(false); })
      .catch(() => setLoading(false));
  }, []);

  return (
    <Table
      loading={loading}
      loadingText="Loading vulnerabilities..."
      header={<Header variant="h2" counter={`(${items.length})`}>Active Vulnerabilities</Header>}
      columnDefinitions={[
        {
          id: "cve",
          header: "CVE ID",
          cell: (item) => (
            <span
              style={{ cursor: onCveClick ? "pointer" : "default", color: "#0972d3" }}
              onClick={() => onCveClick?.(item.cve_id, item.environment)}
            >
              {item.cve_id}
            </span>
          ),
          sortingField: "cve_id",
        },
        {
          id: "severity",
          header: "Severity",
          cell: (item) => (
            <StatusIndicator type={SEV_TYPE[item.severity] ?? "info"}>
              {item.severity}
            </StatusIndicator>
          ),
          sortingField: "severity",
        },
        {
          id: "cvss",
          header: "CVSS",
          cell: (item) => item.cvss_score ?? "—",
          sortingField: "cvss_score",
        },
        {
          id: "env",
          header: "Environment",
          cell: (item) => item.environment,
        },
        {
          id: "fix",
          header: "Fix",
          cell: (item) => (
            item.fix_available === "YES"
              ? <StatusIndicator type="success">Available</StatusIndicator>
              : <Box color="text-status-inactive">No</Box>
          ),
        },
      ]}
      items={items}
      sortingDisabled={false}
      variant="embedded"
      empty={<Box textAlign="center" color="text-body-secondary">No active vulnerabilities</Box>}
    />
  );
}
