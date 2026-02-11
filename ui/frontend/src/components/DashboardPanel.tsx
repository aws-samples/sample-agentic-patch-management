import { useState } from "react";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Tabs from "@cloudscape-design/components/tabs";
import Flashbar from "@cloudscape-design/components/flashbar";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Spinner from "@cloudscape-design/components/spinner";
import KpiBar from "./KpiBar";
import EnvironmentCards from "./EnvironmentCards";
import VulnTable from "./VulnTable";
import ActivityTable from "./ActivityTable";
import ComplianceSummary from "./ComplianceSummary";
import ExplainabilityTable from "./ExplainabilityTable";
// Workflow tracker disabled — component kept for future use
// import WorkflowTracker from "./WorkflowTracker";
import { useDashboard } from "../hooks/useDashboard";

interface Props {
  onChatPrefill?: (text: string) => void;
}

export default function DashboardPanel({ onChatPrefill }: Props) {
  const { data, loading, lastRefreshed, refresh } = useDashboard();
  const [activeTab, setActiveTab] = useState("overview");

  if (loading && !data) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
        <Box variant="p" margin={{ top: "s" }}>Loading dashboard...</Box>
      </Box>
    );
  }

  if (!data) return null;

  return (
    <SpaceBetween size="l">
      {/* Error flashbar */}
      {data.errors.length > 0 && (
        <Flashbar
          items={data.errors.map((err, i) => ({
            type: "error" as const,
            content: err,
            id: `err-${i}`,
            dismissible: true,
          }))}
        />
      )}

      {/* KPI bar — always visible above tabs */}
      <KpiBar data={data} />

      {/* Tabbed content with refresh controls */}
      <div style={{ position: "relative" }}>
        <div style={{ position: "absolute", right: 0, top: "4px", display: "flex", alignItems: "center", gap: "6px", zIndex: 1 }}>
          {lastRefreshed && (
            <Box variant="small" color="text-body-secondary">
              {lastRefreshed.toLocaleTimeString()}
            </Box>
          )}
          <Button iconName="refresh" variant="icon" onClick={() => refresh(true)} loading={loading} ariaLabel="Refresh" />
        </div>
        <Tabs
        activeTabId={activeTab}
        onChange={({ detail }) => setActiveTab(detail.activeTabId)}
        tabs={[
          {
            id: "overview",
            label: "Environments",
            content: (
              <Box padding={{ top: "s" }}>
                <EnvironmentCards
                  items={data.environments}
                  loading={loading}
                  onEnvClick={(env) => onChatPrefill?.(`Show instances in ${env} environment`)}
                />
              </Box>
            ),
          },
          {
            id: "vulnerabilities",
            label: `Vulnerabilities (${Object.values(data.severityCounts).reduce((s, n) => s + n, 0)})`,
            content: (
              <Box padding={{ top: "s" }}>
                <VulnTable
                  items={data.findings}
                  severityCounts={data.severityCounts}
                  loading={loading}
                  onCveClick={(cve, env) => onChatPrefill?.(`Handle ${cve} in ${env} environment`)}
                />
              </Box>
            ),
          },
          {
            id: "activity",
            label: `Activity (${data.activities.length})`,
            content: (
              <Box padding={{ top: "s" }}>
                <ActivityTable
                  items={data.activities}
                  loading={loading}
                  onRowClick={(cve, env) => onChatPrefill?.(`Query compliance reports for ${cve} in ${env}`)}
                />
              </Box>
            ),
          },
          {
            id: "compliance",
            label: "Compliance",
            content: (
              <Box padding={{ top: "s" }}>
                <ComplianceSummary data={data.compliance} reports={data.reportDetails} loading={loading} />
              </Box>
            ),
          },
          {
            id: "explainability",
            label: `Decisions (${data.reportDetails.length})`,
            content: (
              <Box padding={{ top: "s" }}>
                <ExplainabilityTable
                  items={data.reportDetails}
                  loading={loading}
                  onCveClick={(cve, env) => onChatPrefill?.(`Query compliance reports for ${cve} in ${env}`)}
                />
              </Box>
            ),
          },
        ]}
      />
      </div>
    </SpaceBetween>
  );
}
