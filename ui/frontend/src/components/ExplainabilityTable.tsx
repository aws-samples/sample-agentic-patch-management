import { useState } from "react";
import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Pagination from "@cloudscape-design/components/pagination";
import type { ReportDetail } from "../lib/api";

interface Props {
  items: ReportDetail[];
  loading: boolean;
  onCveClick?: (cve: string, env: string) => void;
}

const PAGE_SIZE = 10;

export default function ExplainabilityTable({ items, loading, onCveClick }: Props) {
  const [currentPage, setCurrentPage] = useState(1);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const paged = items.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);

  return (
    <SpaceBetween size="m">
      <Table
        loading={loading}
        loadingText="Loading decision audit trail..."
        header={
          <Header
            variant="h2"
            description="Full audit trail of agent decisions — why each patch was applied, what SLA framework governed it, and the outcome."
          >
            Decision Audit Trail
          </Header>
        }
        columnDefinitions={[
          {
            id: "timestamp",
            header: "When",
            cell: (item) => (
              <Box variant="small">
                {new Date(item.timestamp).toLocaleString()}
              </Box>
            ),
            width: 160,
          },
          {
            id: "cve",
            header: "CVE",
            cell: (item) => (
              <span
                style={{ cursor: onCveClick ? "pointer" : "default", color: "#0972d3", fontWeight: 600 }}
                onClick={() => onCveClick?.(item.cve_id, item.environment)}
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
            id: "team",
            header: "Team",
            cell: (item) => item.team,
          },
          {
            id: "operator",
            header: "Operator",
            cell: (item) => (
              <Box variant="small">{item.operator || "—"}</Box>
            ),
          },
          {
            id: "decision",
            header: "Decision",
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
              <StatusIndicator type={item.sla_met === true ? "success" : item.sla_met === false ? "error" : "pending"}>
                {item.sla_met === true ? "Met" : item.sla_met === false ? "Breached" : "—"}
              </StatusIndicator>
            ),
          },
          {
            id: "detail",
            header: "",
            cell: (item) => (
              <Box>
                <span
                  style={{ cursor: "pointer", color: "#0972d3", fontSize: "12px" }}
                  onClick={() => setExpandedId(expandedId === item.report_id ? null : item.report_id)}
                >
                  {expandedId === item.report_id ? "Hide" : "Details"}
                </span>
              </Box>
            ),
            width: 70,
          },
        ]}
        items={paged}
        variant="container"
        empty={
          <Box textAlign="center" color="text-body-secondary" padding="l">
            No patching decisions recorded yet.
          </Box>
        }
      />

      {/* Expanded detail panel */}
      {expandedId && (
        <DecisionDetail item={items.find((i) => i.report_id === expandedId) ?? null} />
      )}

      {items.length > PAGE_SIZE && (
        <Pagination
          currentPageIndex={currentPage}
          pagesCount={Math.ceil(items.length / PAGE_SIZE)}
          onChange={({ detail }) => setCurrentPage(detail.currentPageIndex)}
        />
      )}
    </SpaceBetween>
  );
}

function DecisionDetail({ item }: { item: ReportDetail | null }) {
  if (!item) return null;

  return (
    <ExpandableSection
      defaultExpanded
      variant="container"
      headerText={`Decision Reasoning — ${item.cve_id} in ${item.environment}`}
    >
      <ColumnLayout columns={2}>
        <SpaceBetween size="s">
          <div>
            <Box variant="awsui-key-label">Vulnerability</Box>
            <Box>{item.cve_id} ({item.severity}{item.cvss_score ? ` / CVSS ${item.cvss_score}` : ""})</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Compliance Framework</Box>
            <Box>{item.sla_source}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">SLA Threshold</Box>
            <Box>{item.sla_hours != null ? `${item.sla_hours} hours` : "N/A"}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Time to Patch</Box>
            <Box>
              {item.time_to_patch_hours != null
                ? `${item.time_to_patch_hours} hours`
                : "N/A"}
              {item.sla_hours != null && item.time_to_patch_hours != null && (
                <span style={{ marginLeft: "8px" }}>
                  <StatusIndicator
                    type={item.time_to_patch_hours <= item.sla_hours ? "success" : "error"}
                  >
                    {item.time_to_patch_hours <= item.sla_hours ? "Within SLA" : "SLA Exceeded"}
                  </StatusIndicator>
                </span>
              )}
            </Box>
          </div>
        </SpaceBetween>

        <SpaceBetween size="s">
          <div>
            <Box variant="awsui-key-label">Decision Type</Box>
            <Box>
              <StatusIndicator type={item.decision === "EMERGENCY" ? "warning" : "info"}>
                {item.decision}
              </StatusIndicator>
              {item.decision === "EMERGENCY" && (
                <Box variant="small" color="text-body-secondary" margin={{ top: "xxs" }}>
                  Severity or SLA urgency triggered immediate patching
                </Box>
              )}
              {item.decision === "SCHEDULED" && (
                <Box variant="small" color="text-body-secondary" margin={{ top: "xxs" }}>
                  Scheduled within maintenance window per SLA policy
                </Box>
              )}
            </Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Scope</Box>
            <Box>{item.instance_count} instances — Team: {item.team}, Product: {item.product}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Execution Result</Box>
            <Box>
              <StatusIndicator type={item.status === "Success" ? "success" : "error"}>
                {item.status}
              </StatusIndicator>
              {" "}— {item.success_count} succeeded, {item.failure_count} failed
            </Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Initiated By</Box>
            <Box>{item.operator || "unknown"}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Report ID</Box>
            <Box variant="small" color="text-body-secondary">{item.report_id}</Box>
          </div>
        </SpaceBetween>
      </ColumnLayout>
    </ExpandableSection>
  );
}
