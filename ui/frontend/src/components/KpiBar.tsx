import Container from "@cloudscape-design/components/container";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Box from "@cloudscape-design/components/box";
import type { DashboardData } from "../lib/api";

interface Props {
  data: DashboardData;
}

export default function KpiBar({ data }: Props) {
  const totalInstances = data.environments.reduce((s, e) => s + e.total, 0);
  const onlineInstances = data.environments.reduce((s, e) => s + e.online, 0);
  const criticalVulns = data.severityCounts["CRITICAL"] ?? 0;
  const highVulns = data.severityCounts["HIGH"] ?? 0;
  const slaRate = data.compliance?.sla_rate_percent ?? 0;
  const totalReports = data.compliance?.total_reports ?? 0;
  const slaBreached = data.compliance?.sla_breached ?? 0;

  return (
    <Container>
      <ColumnLayout columns={5} variant="text-grid">
        <KpiItem
          label="Instances"
          value={`${onlineInstances} / ${totalInstances}`}
          status={onlineInstances === totalInstances ? "success" : "warning"}
          detail="online"
        />
        <KpiItem
          label="Critical Vulns"
          value={criticalVulns.toString()}
          status={criticalVulns === 0 ? "success" : "error"}
          detail={criticalVulns === 0 ? "none found" : "action needed"}
        />
        <KpiItem
          label="High Vulns"
          value={highVulns.toString()}
          status={highVulns === 0 ? "success" : "warning"}
          detail={`of ${data.findings.length} total`}
        />
        <KpiItem
          label="SLA Compliance"
          value={totalReports > 0 ? `${slaRate}%` : "—"}
          status={slaRate >= 95 ? "success" : slaRate >= 80 ? "warning" : "error"}
          detail={totalReports > 0 ? `${totalReports} reports (30d)` : "no data"}
        />
        <KpiItem
          label="SLA Breaches"
          value={slaBreached.toString()}
          status={slaBreached === 0 ? "success" : "error"}
          detail="last 30 days"
        />
      </ColumnLayout>
    </Container>
  );
}

function KpiItem({
  label,
  value,
  detail,
}: {
  label: string;
  value: string;
  status: "success" | "warning" | "error";
  detail: string;
}) {
  return (
    <div>
      <Box variant="awsui-key-label" margin={{ bottom: "xxs" }}>{label}</Box>
      <Box variant="awsui-value-large">{value}</Box>
      <Box variant="small" color="text-body-secondary">
        {detail}
      </Box>
    </div>
  );
}
