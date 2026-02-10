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
  const offlineInstances = totalInstances - onlineInstances;
  const criticalVulns = data.severityCounts["CRITICAL"] ?? 0;
  const highVulns = data.severityCounts["HIGH"] ?? 0;
  const mediumVulns = data.severityCounts["MEDIUM"] ?? 0;
  const totalVulns = data.findings.length;
  const slaRate = data.compliance?.sla_rate_percent ?? 0;
  const totalReports = data.compliance?.total_reports ?? 0;
  const slaBreached = data.compliance?.sla_breached ?? 0;
  const envCount = data.environments.length;

  return (
    <Container>
      <ColumnLayout columns={6} variant="text-grid">
        <KpiItem
          label="Environments"
          value={envCount.toString()}
          detail={`${totalInstances} instances · ${offlineInstances > 0 ? `${offlineInstances} offline` : "all online"}`}
        />
        <KpiItem
          label="Instances Online"
          value={`${onlineInstances} / ${totalInstances}`}
          detail={onlineInstances === totalInstances ? "all healthy" : `${offlineInstances} need attention`}
        />
        <KpiItem
          label="Critical Vulnerabilities"
          value={criticalVulns.toString()}
          detail={criticalVulns === 0 ? "none detected" : "immediate action needed"}
        />
        <KpiItem
          label="High Vulnerabilities"
          value={highVulns.toString()}
          detail={`${mediumVulns} medium · ${totalVulns} total`}
        />
        <KpiItem
          label="SLA Compliance"
          value={totalReports > 0 ? `${slaRate}%` : "—"}
          detail={totalReports > 0 ? `${totalReports} reports · 30 days` : "no reports yet"}
        />
        <KpiItem
          label="SLA Breaches"
          value={slaBreached.toString()}
          detail={slaBreached === 0 ? "none in 30 days" : "review required"}
        />
      </ColumnLayout>
    </Container>
  );
}

function KpiItem({ label, detail, value }: { label: string; value: string; detail: string }) {
  return (
    <div style={{ textAlign: "center" }}>
      <Box variant="awsui-key-label">{label}</Box>
      <div style={{ fontSize: "24px", fontWeight: 700, lineHeight: 1.3, margin: "4px 0" }}>
        {value}
      </div>
      <Box variant="small" color="text-body-secondary">
        {detail}
      </Box>
    </div>
  );
}
