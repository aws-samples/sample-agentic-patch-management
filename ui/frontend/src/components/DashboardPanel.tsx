import SpaceBetween from "@cloudscape-design/components/space-between";
import EnvironmentCards from "./EnvironmentCards";
import VulnTable from "./VulnTable";
import ActivityTable from "./ActivityTable";
import ComplianceSummary from "./ComplianceSummary";

interface Props {
  onChatPrefill?: (text: string) => void;
}

export default function DashboardPanel({ onChatPrefill }: Props) {
  return (
    <SpaceBetween size="l">
      <EnvironmentCards
        onEnvClick={(env) => onChatPrefill?.(`Show instances in ${env} environment`)}
      />
      <VulnTable
        onCveClick={(cve, env) =>
          onChatPrefill?.(`Handle ${cve} in ${env} environment`)
        }
      />
      <ActivityTable
        onRowClick={(cve) =>
          onChatPrefill?.(`Show compliance report for ${cve}`)
        }
      />
      <ComplianceSummary />
    </SpaceBetween>
  );
}
