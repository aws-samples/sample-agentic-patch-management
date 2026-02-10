import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Box from "@cloudscape-design/components/box";
import type { ComplianceSummary as ComplianceData } from "../lib/api";

interface Props {
  data: ComplianceData | null;
  loading: boolean;
}

export default function ComplianceSummary({ data, loading }: Props) {
  if (loading) return null; // KPI bar handles the loading state
  if (!data || !data.total_reports) {
    return (
      <Container header={<Header variant="h2">Compliance</Header>}>
        <Box textAlign="center" color="text-body-secondary">No compliance data yet</Box>
      </Container>
    );
  }

  const rateType = data.sla_rate_percent >= 95 ? "success"
    : data.sla_rate_percent >= 80 ? "warning" : "error";

  return (
    <Container header={<Header variant="h2">Compliance (30d)</Header>}>
      <KeyValuePairs
        columns={2}
        items={[
          {
            label: "SLA Rate",
            value: (
              <StatusIndicator type={rateType}>
                {data.sla_rate_percent}%
              </StatusIndicator>
            ),
          },
          {
            label: "Reports",
            value: data.total_reports.toString(),
          },
          {
            label: "Met",
            value: (
              <StatusIndicator type="success">{data.sla_met}</StatusIndicator>
            ),
          },
          {
            label: "Breached",
            value: data.sla_breached > 0 ? (
              <StatusIndicator type="error">{data.sla_breached}</StatusIndicator>
            ) : (
              <StatusIndicator type="success">0</StatusIndicator>
            ),
          },
        ]}
      />
    </Container>
  );
}
