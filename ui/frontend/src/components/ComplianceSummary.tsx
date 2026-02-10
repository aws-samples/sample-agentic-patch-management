import { useEffect, useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Spinner from "@cloudscape-design/components/spinner";
import Box from "@cloudscape-design/components/box";
import type { ComplianceSummary as ComplianceData } from "../lib/api";
import { api } from "../lib/api";

export default function ComplianceSummary() {
  const [data, setData] = useState<ComplianceData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.compliance().then((d) => { setData(d.summary); setLoading(false); })
      .catch(() => setLoading(false));
  }, []);

  if (loading) return <Spinner size="large" />;
  if (!data || !data.total_reports) {
    return (
      <Container header={<Header variant="h2">Compliance Summary</Header>}>
        <Box textAlign="center" color="text-body-secondary">No compliance data yet</Box>
      </Container>
    );
  }

  const rateType = data.sla_rate_percent >= 95 ? "success"
    : data.sla_rate_percent >= 80 ? "warning" : "error";

  return (
    <Container header={<Header variant="h2">Compliance Summary (30 days)</Header>}>
      <KeyValuePairs
        columns={4}
        items={[
          {
            label: "SLA Compliance",
            value: (
              <StatusIndicator type={rateType}>
                {data.sla_rate_percent}%
              </StatusIndicator>
            ),
          },
          {
            label: "Total Reports",
            value: data.total_reports.toString(),
          },
          {
            label: "SLA Met",
            value: (
              <StatusIndicator type="success">
                {data.sla_met}
              </StatusIndicator>
            ),
          },
          {
            label: "SLA Breached",
            value: data.sla_breached > 0 ? (
              <StatusIndicator type="error">
                {data.sla_breached}
              </StatusIndicator>
            ) : (
              <StatusIndicator type="success">0</StatusIndicator>
            ),
          },
        ]}
      />
    </Container>
  );
}
