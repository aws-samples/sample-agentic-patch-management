import { useMemo, useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table from "@cloudscape-design/components/table";
import Select from "@cloudscape-design/components/select";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Box from "@cloudscape-design/components/box";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import Pagination from "@cloudscape-design/components/pagination";
import type { ComplianceSummary as ComplianceData, ReportDetail } from "../lib/api";

interface Props {
  data: ComplianceData | null;
  reports: ReportDetail[];
  loading: boolean;
}

const PAGE_SIZE = 10;

export default function ComplianceSummary({ data, reports, loading }: Props) {
  const [teamFilter, setTeamFilter] = useState<string | null>(null);
  const [sevFilter, setSevFilter] = useState<string | null>(null);
  const [envFilter, setEnvFilter] = useState<string | null>(null);
  const [currentPage, setCurrentPage] = useState(1);

  // Unique filter options
  const teams = useMemo(() => [...new Set(reports.map((r) => r.team))].sort(), [reports]);
  const severities = useMemo(() => [...new Set(reports.map((r) => r.severity))].sort(), [reports]);
  const environments = useMemo(() => [...new Set(reports.map((r) => r.environment))].sort(), [reports]);

  // Filtered reports
  const filtered = useMemo(() => {
    let result = reports;
    if (teamFilter) result = result.filter((r) => r.team === teamFilter);
    if (sevFilter) result = result.filter((r) => r.severity === sevFilter);
    if (envFilter) result = result.filter((r) => r.environment === envFilter);
    return result;
  }, [reports, teamFilter, sevFilter, envFilter]);

  // Filtered stats
  const filteredMet = filtered.filter((r) => r.sla_met === true).length;
  const filteredBreached = filtered.filter((r) => r.sla_met === false).length;
  const filteredRate = filtered.length > 0 ? Math.round((filteredMet / filtered.length) * 100 * 10) / 10 : 0;

  const paged = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);

  if (loading) return null;
  if (!data || !data.total_reports) {
    return (
      <Container header={<Header variant="h2">Compliance</Header>}>
        <Box textAlign="center" color="text-body-secondary" padding="l">
          No compliance data yet. Run a patching workflow to generate reports.
        </Box>
      </Container>
    );
  }

  return (
    <SpaceBetween size="l">
      {/* Overall SLA bar */}
      <Container header={<Header variant="h2">SLA Compliance — Last 30 Days</Header>}>
        <ColumnLayout columns={2}>
          <ProgressBar
            value={data.sla_rate_percent}
            status={data.sla_rate_percent >= 95 ? "success" : data.sla_rate_percent >= 80 ? "in-progress" : "error"}
            description={`${data.sla_met} met / ${data.sla_breached} breached out of ${data.total_reports} total`}
            label="SLA Compliance Rate"
            resultText={`${data.sla_rate_percent}%`}
          />
          <ColumnLayout columns={3} variant="text-grid">
            <div style={{ textAlign: "center" }}>
              <Box variant="awsui-key-label">Total</Box>
              <div style={{ fontSize: "28px", fontWeight: 700 }}>{data.total_reports}</div>
            </div>
            <div style={{ textAlign: "center" }}>
              <Box variant="awsui-key-label">Met</Box>
              <div style={{ fontSize: "28px", fontWeight: 700, color: "#037f0c" }}>{data.sla_met}</div>
            </div>
            <div style={{ textAlign: "center" }}>
              <Box variant="awsui-key-label">Breached</Box>
              <div style={{ fontSize: "28px", fontWeight: 700, color: data.sla_breached > 0 ? "#d91515" : "#037f0c" }}>
                {data.sla_breached}
              </div>
            </div>
          </ColumnLayout>
        </ColumnLayout>
      </Container>

      {/* Filterable reports table */}
      <Table
        header={
          <Header
            variant="h2"
            counter={`(${filtered.length}${filtered.length !== reports.length ? ` of ${reports.length}` : ""})`}
            description={filtered.length !== reports.length ? `Filtered: ${filteredRate}% SLA rate — ${filteredMet} met, ${filteredBreached} breached` : undefined}
          >
            Compliance Reports
          </Header>
        }
        filter={
          <div style={{ display: "flex", gap: "12px", flexWrap: "wrap" }}>
            <div style={{ minWidth: "150px" }}>
              <Select
                selectedOption={teamFilter ? { value: teamFilter, label: teamFilter } : null}
                onChange={({ detail }) => { setTeamFilter(detail.selectedOption?.value ?? null); setCurrentPage(1); }}
                options={teams.map((t) => ({ value: t, label: t }))}
                placeholder="All teams"
                selectedAriaLabel="Selected"
              />
            </div>
            <div style={{ minWidth: "150px" }}>
              <Select
                selectedOption={sevFilter ? { value: sevFilter, label: sevFilter } : null}
                onChange={({ detail }) => { setSevFilter(detail.selectedOption?.value ?? null); setCurrentPage(1); }}
                options={severities.map((s) => ({ value: s, label: s }))}
                placeholder="All severities"
                selectedAriaLabel="Selected"
              />
            </div>
            <div style={{ minWidth: "150px" }}>
              <Select
                selectedOption={envFilter ? { value: envFilter, label: envFilter } : null}
                onChange={({ detail }) => { setEnvFilter(detail.selectedOption?.value ?? null); setCurrentPage(1); }}
                options={environments.map((e) => ({ value: e, label: e }))}
                placeholder="All environments"
                selectedAriaLabel="Selected"
              />
            </div>
          </div>
        }
        columnDefinitions={[
          {
            id: "timestamp",
            header: "When",
            cell: (item) => new Date(item.timestamp).toLocaleString(),
            width: 170,
          },
          { id: "cve", header: "CVE", cell: (item) => item.cve_id },
          { id: "env", header: "Env", cell: (item) => item.environment },
          { id: "team", header: "Team", cell: (item) => item.team },
          { id: "severity", header: "Severity", cell: (item) => item.severity },
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
            id: "sla_source",
            header: "Framework",
            cell: (item) => item.sla_source,
          },
          {
            id: "time",
            header: "Patch Time",
            cell: (item) => item.time_to_patch_hours != null ? `${item.time_to_patch_hours}h` : "—",
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
            id: "result",
            header: "Result",
            cell: (item) => (
              <StatusIndicator type={item.status === "Success" ? "success" : "error"}>
                {item.success_count}/{item.instance_count}
              </StatusIndicator>
            ),
          },
        ]}
        items={paged}
        variant="container"
        pagination={
          filtered.length > PAGE_SIZE ? (
            <Pagination
              currentPageIndex={currentPage}
              pagesCount={Math.ceil(filtered.length / PAGE_SIZE)}
              onChange={({ detail }) => setCurrentPage(detail.currentPageIndex)}
            />
          ) : undefined
        }
        empty={<Box textAlign="center" color="text-body-secondary">No reports match filters</Box>}
      />
    </SpaceBetween>
  );
}
