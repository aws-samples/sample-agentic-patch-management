import type { DashboardData } from "../lib/api";
import { RadialGauge } from "./ui/RadialGauge";

interface Props {
  data: DashboardData;
  accountFilter?: string;
}

export default function KpiBar({ data, accountFilter }: Props) {
  const totalInstances = data.environments.reduce((s, e) => s + e.total, 0);
  const onlineInstances = data.environments.reduce((s, e) => s + e.online, 0);
  const criticalVulns = data.severityCounts["CRITICAL"] ?? 0;
  const totalReports = data.compliance?.total_reports ?? 0;
  const slaBreached = data.compliance?.sla_breached ?? 0;
  const slaMet = data.compliance?.sla_met ?? 0;

  const envCompliance = data.environments.reduce(
    (acc, e) => {
      if (e.patch_compliance) {
        acc.compliant += e.patch_compliance.compliant_instances;
        acc.scanned += e.patch_compliance.scanned_instances;
        acc.missing += e.patch_compliance.missing_patches;
      }
      return acc;
    },
    { compliant: 0, scanned: 0, missing: 0 }
  );
  // Use total instances as denominator — unscanned instances count as non-compliant
  const complianceRate = totalInstances > 0
    ? Math.round((envCompliance.compliant / totalInstances) * 100 * 10) / 10
    : null;
  const slaRate = totalReports > 0
    ? Math.round((slaMet / totalReports) * 100 * 10) / 10
    : null;

  const uniqueAccounts = Array.from(new Set((data.scopes ?? []).map((s) => s.accountId)));
  const uniqueRegions = Array.from(new Set((data.scopes ?? []).map((s) => s.region)));

  // When filtered to a specific account, show that account's regions only
  const filteredRegions = accountFilter && accountFilter !== "ALL"
    ? Array.from(new Set((data.scopes ?? []).filter((s) => s.accountId === accountFilter).map((s) => s.region)))
    : uniqueRegions;

  return (
    <div className="space-y-3">
      {/* Scope breadcrumb */}
      <div className="bg-surface-card border border-edge rounded-md px-2.5 py-1 inline-flex items-center gap-2 text-[11px] font-mono text-fg-muted">
        <svg className="w-3 h-3 text-accent/50 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path strokeLinecap="round" strokeLinejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z" /></svg>
        {accountFilter && accountFilter !== "ALL" ? (
          <>
            <span>Account:</span>
            <span className="text-accent font-mono">{accountFilter}</span>
            <span className="text-fg-faint">&middot;</span>
            <span className="text-fg-faint">{filteredRegions.length === 1 ? filteredRegions[0] : `${filteredRegions.length} regions`}</span>
          </>
        ) : (
          <>
            <span>All accounts</span>
            <span className="text-fg-faint">&middot;</span>
            <span className="text-fg-secondary">{uniqueAccounts.length} accounts</span>
            <span className="text-fg-faint">&middot;</span>
            <span className="text-fg-faint">{uniqueRegions.length} regions</span>
          </>
        )}
      </div>

      {/* Metrics — numbers for counts, inline visuals for rates */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <CountCard
          label="Fleet"
          value={`${onlineInstances}/${totalInstances}`}
          sub={onlineInstances === totalInstances ? "all online" : `${totalInstances - onlineInstances} offline`}
          color={onlineInstances === totalInstances ? "emerald" : "red"}
        />
        <CountCard
          label="Critical CVEs"
          value={criticalVulns.toString()}
          sub={criticalVulns > 0 ? "require patching" : "none found"}
          color={criticalVulns > 0 ? "red" : "emerald"}
        />
        <RateCard
          label="Patch Compliance"
          rate={complianceRate}
          detail={complianceRate !== null
            ? `${envCompliance.compliant}/${totalInstances} compliant` + (envCompliance.scanned < totalInstances ? ` (${totalInstances - envCompliance.scanned} unscanned)` : '')
            : "not scanned"}
          fillColor={complianceRate === null ? "#475569" : complianceRate >= 90 ? "#34d399" : complianceRate >= 70 ? "#fbbf24" : "#f87171"}
          trackColor="var(--color-edge)"
        />
        <RateCard
          label="SLA Compliance"
          rate={slaRate}
          detail={totalReports > 0
            ? `${slaMet} met / ${slaBreached} breached / ${totalReports} total`
            : "no reports yet"}
          fillColor={slaRate === null ? "#475569" : slaRate >= 95 ? "#34d399" : slaRate >= 80 ? "#fbbf24" : "#f87171"}
          trackColor="var(--color-edge)"
        />
      </div>
    </div>
  );
}

/* ── Count Card — raw number, color-coded ────────────────────── */

const BORDER: Record<string, string> = {
  emerald: "border-l-emerald-400/50",
  amber: "border-l-amber-400/50",
  red: "border-l-red-400/50",
  slate: "border-l-slate-500/30",
};
const TEXT: Record<string, string> = {
  emerald: "text-emerald-400",
  amber: "text-amber-400",
  red: "text-red-400",
  slate: "text-fg-secondary",
};

function CountCard({ label, value, sub, color }: {
  label: string; value: string; sub: string; color: string;
}) {
  return (
    <div className={`bg-surface-card rounded-lg border border-edge border-l-[3px] ${BORDER[color]} px-3.5 py-3`}>
      <div className="text-[10px] font-semibold text-fg-muted uppercase tracking-wider">{label}</div>
      <div className={`text-2xl font-bold font-mono mt-1 leading-none ${TEXT[color]}`}>{value}</div>
      <div className="text-[11px] text-fg-faint mt-1.5">{sub}</div>
    </div>
  );
}

/* ── Rate Card — number + radial gauge ───────────────────────── */

function RateCard({ label, rate, detail, fillColor, trackColor }: {
  label: string; rate: number | null; detail: string; fillColor: string; trackColor: string;
}) {
  const displayRate = rate !== null ? `${rate}%` : "\u2014";

  return (
    <div className="bg-surface-card rounded-lg border border-edge px-3.5 py-3 flex items-center gap-3">
      <RadialGauge rate={rate} size={44} strokeWidth={4} fillColor={fillColor} trackColor={trackColor} />
      {/* Text */}
      <div className="min-w-0">
        <div className="text-[10px] font-semibold text-fg-muted uppercase tracking-wider">{label}</div>
        <div className="text-lg font-bold font-mono mt-0.5 leading-none text-fg">{displayRate}</div>
        <div className="text-[11px] text-fg-faint mt-1">{detail}</div>
      </div>
    </div>
  );
}
