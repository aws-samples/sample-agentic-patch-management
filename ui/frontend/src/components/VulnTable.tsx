import { useState, useMemo } from "react";
import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Box from "@cloudscape-design/components/box";
import Pagination from "@cloudscape-design/components/pagination";
import Select from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import type { VulnerabilityFinding } from "../lib/api";

const SEV_TYPE: Record<string, "error" | "warning" | "info" | "stopped"> = {
  CRITICAL: "error",
  HIGH: "warning",
  MEDIUM: "info",
  LOW: "stopped",
};

const PAGE_SIZE = 10;

const SEVERITY_OPTIONS = [
  { label: "All severities", value: "ALL" },
  { label: "CRITICAL", value: "CRITICAL" },
  { label: "HIGH", value: "HIGH" },
  { label: "MEDIUM", value: "MEDIUM" },
  { label: "LOW", value: "LOW" },
];

interface Props {
  items: VulnerabilityFinding[];
  severityCounts: Record<string, number>;
  loading: boolean;
  onCveClick?: (cveId: string, env: string) => void;
}

export default function VulnTable({ items, severityCounts, loading, onCveClick }: Props) {
  const [currentPage, setCurrentPage] = useState(1);
  const [severityFilter, setSeverityFilter] = useState(SEVERITY_OPTIONS[0]);

  const filtered = useMemo(() => {
    if (severityFilter.value === "ALL") return items;
    return items.filter((i) => i.severity === severityFilter.value);
  }, [items, severityFilter]);

  const paged = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);

  // Build counter string like "(2 Critical, 12 High, 36 Medium)"
  const counterParts: string[] = [];
  for (const sev of ["CRITICAL", "HIGH", "MEDIUM", "LOW"]) {
    const count = severityCounts[sev];
    if (count) counterParts.push(`${count} ${sev[0]}${sev.slice(1).toLowerCase()}`);
  }
  const counter = counterParts.length > 0 ? `(${counterParts.join(", ")})` : `(${items.length})`;
  const totalFromCounts = Object.values(severityCounts).reduce((s, n) => s + n, 0);
  const isTruncated = totalFromCounts > items.length;

  return (
    <Table
      loading={loading}
      loadingText="Loading vulnerabilities..."
      header={
        <Header
          variant="h2"
          counter={counter}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Select
                selectedOption={severityFilter}
                onChange={({ detail }) => {
                  setSeverityFilter(detail.selectedOption as typeof severityFilter);
                  setCurrentPage(1);
                }}
                options={SEVERITY_OPTIONS}
              />
            </SpaceBetween>
          }
        >
          Active Vulnerabilities
        </Header>
      }
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
        },
        {
          id: "env",
          header: "Environment",
          cell: (item) => item.environment,
        },
        {
          id: "fix",
          header: "Fix",
          cell: (item) =>
            item.fix_available === "YES" ? (
              <StatusIndicator type="success">Available</StatusIndicator>
            ) : (
              <Box color="text-status-inactive">No</Box>
            ),
        },
      ]}
      items={paged}
      pagination={
        <Pagination
          currentPageIndex={currentPage}
          pagesCount={Math.ceil(filtered.length / PAGE_SIZE)}
          onChange={({ detail }) => setCurrentPage(detail.currentPageIndex)}
        />
      }
      variant="container"
      empty={
        isTruncated && filtered.length === 0 && severityFilter.value !== "ALL" ? (
          <Box textAlign="center" color="text-body-secondary" padding="l">
            {severityCounts[severityFilter.value] ?? 0} {severityFilter.value.toLowerCase()} findings exist but are beyond the loaded set ({items.length} of {totalFromCounts} shown).
            Use the chat to query: "Show {severityFilter.value.toLowerCase()} vulnerabilities"
          </Box>
        ) : (
          <Box textAlign="center" color="text-body-secondary">No active vulnerabilities</Box>
        )
      }
    />
  );
}
