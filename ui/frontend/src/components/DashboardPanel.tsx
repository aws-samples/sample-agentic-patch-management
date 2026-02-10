import SpaceBetween from "@cloudscape-design/components/space-between";
import Grid from "@cloudscape-design/components/grid";
import Flashbar from "@cloudscape-design/components/flashbar";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Spinner from "@cloudscape-design/components/spinner";
import KpiBar from "./KpiBar";
import EnvironmentCards from "./EnvironmentCards";
import VulnTable from "./VulnTable";
import ActivityTable from "./ActivityTable";
import ComplianceSummary from "./ComplianceSummary";
import { useDashboard } from "../hooks/useDashboard";

interface Props {
  onChatPrefill?: (text: string) => void;
}

export default function DashboardPanel({ onChatPrefill }: Props) {
  const { data, loading, lastRefreshed, refresh } = useDashboard();

  // Refresh when parent signals (e.g., after chat operation completes)
  // This is a simple approach — refreshSignal changes trigger useEffect in parent
  // For now, the 60s auto-refresh handles it

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

      {/* Refresh bar */}
      <Box float="right">
        <SpaceBetween direction="horizontal" size="xs">
          {lastRefreshed && (
            <Box variant="small" color="text-body-secondary" padding={{ top: "xxs" }}>
              Last updated: {lastRefreshed.toLocaleTimeString()}
            </Box>
          )}
          <Button iconName="refresh" variant="icon" onClick={refresh} ariaLabel="Refresh dashboard" />
        </SpaceBetween>
      </Box>

      {/* KPI summary strip */}
      <KpiBar data={data} />

      {/* Two-column layout */}
      <Grid
        gridDefinition={[
          { colspan: { default: 12, m: 5 } },
          { colspan: { default: 12, m: 7 } },
        ]}
      >
        {/* Left column: status-oriented */}
        <SpaceBetween size="l">
          <EnvironmentCards
            items={data.environments}
            loading={loading}
            onEnvClick={(env) => onChatPrefill?.(`Show instances in ${env} environment`)}
          />
          <ComplianceSummary data={data.compliance} loading={loading} />
        </SpaceBetween>

        {/* Right column: data-dense */}
        <SpaceBetween size="l">
          <VulnTable
            items={data.findings}
            severityCounts={data.severityCounts}
            loading={loading}
            onCveClick={(cve, env) => onChatPrefill?.(`Handle ${cve} in ${env} environment`)}
          />
          <ActivityTable
            items={data.activities}
            loading={loading}
            onRowClick={(cve) => onChatPrefill?.(`Show compliance report for ${cve}`)}
          />
        </SpaceBetween>
      </Grid>
    </SpaceBetween>
  );
}
