import { useState, useEffect } from "react";
import { Transition } from "@headlessui/react";
import { ArrowPathIcon, ExclamationCircleIcon, XMarkIcon } from "@heroicons/react/24/outline";
import KpiBar from "./KpiBar";
import StatusBar from "./StatusBar";
import EnvironmentCards from "./EnvironmentCards";
import VulnTable from "./VulnTable";
import ActivityTable from "./ActivityTable";
import ComplianceSummary from "./ComplianceSummary";
import ExplainabilityTable from "./ExplainabilityTable";
import RunningOpsBadge from "./RunningOpsBadge";
import { KpiSkeleton, CardsSkeleton } from "./ui/Skeleton";
import ExportButton from "./ui/ExportButton";
import { useDashboard } from "../hooks/useDashboard";

interface Props {
  onChatPrefill?: (text: string) => void;
  role?: "operator" | "viewer";
}

const TABS = [
  { id: "overview", label: "Environments" },
  { id: "vulnerabilities", label: "Vulnerabilities" },
  { id: "activity", label: "Activity" },
  { id: "compliance", label: "Compliance" },
  { id: "explainability", label: "Decisions" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function DashboardPanel({ onChatPrefill, role = "operator" }: Props) {
  const { data, loading, lastRefreshed, refresh } = useDashboard();
  const [activeTab, setActiveTab] = useState<TabId>("overview");
  const [dismissedErrors, setDismissedErrors] = useState<Set<number>>(new Set());
  const [accountFilter, setAccountFilter] = useState<string>("ALL");

  // Listen for keyboard shortcut events from App
  useEffect(() => {
    const handleRefresh = () => refresh(true);
    const handleTab = (e: Event) => {
      const index = (e as CustomEvent).detail as number;
      if (index >= 1 && index <= TABS.length) {
        setActiveTab(TABS[index - 1].id);
      }
    };
    document.addEventListener("patchy:refresh", handleRefresh);
    document.addEventListener("patchy:tab", handleTab);
    return () => {
      document.removeEventListener("patchy:refresh", handleRefresh);
      document.removeEventListener("patchy:tab", handleTab);
    };
  }, [refresh]);

  if (loading && !data) {
    return (
      <div className="space-y-4 pt-1">
        <KpiSkeleton />
        <CardsSkeleton count={4} />
      </div>
    );
  }

  if (!data) return null;

  const filteredFindings = accountFilter === "ALL"
    ? data.findings
    : data.findings.filter((f) => f.accounts?.includes(accountFilter));
  const vulnCount = accountFilter === "ALL"
    ? Object.values(data.severityCounts).reduce((s, n) => s + n, 0)
    : filteredFindings.length;

  // Filter environments by selected account
  let filteredEnvironments = data.environments;
  if (accountFilter !== "ALL") {
    filteredEnvironments = data.environments
      .filter((e) => e.accounts?.includes(accountFilter))
      .map((e) => {
        const pa = e.per_account?.[accountFilter];
        if (!pa) return e;
        return { ...e, total: pa.total, online: pa.online, offline: pa.offline, patch_compliance: pa.patch_compliance };
      });
    const envVulns: Record<string, Record<string, number>> = {};
    for (const f of filteredFindings) {
      // Each CVE entry has `environments[]` (union across affected instances);
      // the legacy `environment` singular only carries the first env seen and
      // would under-count the other environments hit by the same CVE.
      const envList = (f.environments && f.environments.length > 0)
        ? f.environments
        : [f.environment];
      for (const env of envList) {
        if (!envVulns[env]) envVulns[env] = {};
        envVulns[env][f.severity] = (envVulns[env][f.severity] ?? 0) + 1;
      }
    }
    filteredEnvironments = filteredEnvironments.map((e) => {
      const vulns = envVulns[e.environment] ?? {};
      return { ...e, vulns, vuln_total: Object.values(vulns).reduce((s, n) => s + n, 0) };
    });
  }

  return (
    <div className="space-y-4">
      {/* Error alerts */}
      {data.errors.filter((_, i) => !dismissedErrors.has(i)).map((err, i) => (
        <div key={i} className="flex items-start gap-2.5 p-3 bg-red-500/10 border border-red-500/20 rounded-xl text-sm text-red-400 animate-fade-in">
          <ExclamationCircleIcon className="w-5 h-5 text-red-400 shrink-0 mt-0.5" />
          <span className="flex-1 leading-relaxed">{err}</span>
          <button onClick={() => setDismissedErrors((s) => new Set(s).add(i))} className="p-0.5 rounded-md text-red-500 hover:text-red-400 hover:bg-red-500/10 transition">
            <XMarkIcon className="w-4 h-4" />
          </button>
        </div>
      ))}

      {/* Warning banners (e.g., Explorer sync missing).
          Light mode uses higher-contrast amber (amber-700 on amber-50);
          dark mode keeps the existing tuned-for-dark palette. */}
      {data.warnings?.map((w, i) => (
        <div key={`warn-${i}`} className="flex items-start gap-2.5 p-3 bg-amber-50 border border-amber-200 rounded-xl text-sm text-amber-800 dark:bg-amber-500/10 dark:border-amber-500/20 dark:text-amber-300 animate-fade-in">
          <ExclamationCircleIcon className="w-5 h-5 text-amber-600 dark:text-amber-400 shrink-0 mt-0.5" />
          <div className="flex-1 leading-relaxed">
            <span className="font-semibold">{w.title}</span>
            <p className="mt-0.5 text-amber-700 dark:text-amber-400/80">{w.message}</p>
          </div>
        </div>
      ))}

      {/* KPI bar */}
      <KpiBar data={data} accountFilter={accountFilter} />

      {/* Status bar */}
      <StatusBar
        lastRefreshed={lastRefreshed}
        runningOpsCount={(data as any).runningOperations?.length ?? 0}
        slaRate={data.compliance?.sla_rate_percent ?? null}
      />

      {/* Tabs + refresh */}
      <div className="flex items-center justify-between border-b border-edge">
        <div className="flex gap-0 -mb-px overflow-x-auto scrollbar-none" role="tablist">
          {TABS.map((tab) => {
            const isActive = activeTab === tab.id;
            let count: number | null = null;
            if (tab.id === "vulnerabilities") count = vulnCount;
            if (tab.id === "activity") count = data.activities.length;
            if (tab.id === "explainability") count = data.reportDetails.length;
            return (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`px-4 py-2.5 text-sm font-medium border-b-2 transition whitespace-nowrap flex items-center gap-1.5 ${
                  isActive
                    ? "border-teal-400 text-accent"
                    : "border-transparent text-fg-muted hover:text-fg-secondary hover:border-slate-600"
                }`}
              >
                {tab.label}
                {count != null && count > 0 && (
                  <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded-full min-w-[20px] text-center ${
                    isActive ? "bg-accent-muted text-accent" : "bg-hover text-fg-muted"
                  }`}>
                    {count > 999 ? `${(count / 1000).toFixed(1)}k` : count}
                  </span>
                )}
              </button>
            );
          })}
        </div>
        <div className="flex items-center gap-2 pb-1">
          {(() => {
            // Scopes is now (account × region) pairs — dedupe by accountId for the dropdown.
            const uniqueAccounts = Array.from(new Set((data.scopes ?? []).map((s) => s.accountId)));
            if (uniqueAccounts.length <= 1) return null;
            return (
              <select
                value={accountFilter}
                onChange={(e) => setAccountFilter(e.target.value)}
                className="text-[11px] bg-input border border-edge rounded-lg px-2 py-1 text-fg-secondary font-mono focus:outline-none focus:ring-1 focus:ring-accent-border transition"
              >
                <option value="ALL" className="bg-surface-raised">All accounts</option>
                {uniqueAccounts.map((aid) => (
                  <option key={aid} value={aid} className="bg-surface-raised">{aid}</option>
                ))}
              </select>
            );
          })()}
          <RunningOpsBadge operations={data.runningOperations} />
          {["vulnerabilities", "activity", "compliance", "explainability"].includes(activeTab) && (
            <ExportButton
              data={
                activeTab === "vulnerabilities"
                  ? (filteredFindings as unknown as Record<string, unknown>[])
                  : activeTab === "activity"
                  ? (data.activities as unknown as Record<string, unknown>[])
                  : (data.reportDetails as unknown as Record<string, unknown>[])
              }
              filename={
                activeTab === "vulnerabilities"
                  ? "vulnerabilities"
                  : activeTab === "activity"
                  ? "activity"
                  : "compliance-reports"
              }
            />
          )}
          {lastRefreshed && (
            <span className="text-[11px] text-fg-faint font-mono">{lastRefreshed.toLocaleTimeString()}</span>
          )}
          <button
            onClick={() => refresh(true)}
            disabled={loading}
            className="p-1.5 rounded-lg hover:bg-hover text-fg-muted hover:text-fg-secondary transition disabled:opacity-40"
            aria-label="Refresh"
          >
            <ArrowPathIcon className={`w-4 h-4 ${loading ? "animate-spin" : ""}`} />
          </button>
        </div>
      </div>

      {/* Tab content */}
      <div className="pt-1">
        <Transition
          as="div"
          show={activeTab === "overview"}
          enter="transition-opacity duration-150"
          enterFrom="opacity-0"
          enterTo="opacity-100"
          leave="transition-opacity duration-100"
          leaveFrom="opacity-100"
          leaveTo="opacity-0"
        >
          <EnvironmentCards
            items={filteredEnvironments}
            loading={loading}
            onEnvClick={(env) => onChatPrefill?.(`Show instances in ${env} environment`)}
            onQuickAction={role === "operator" ? (text) => onChatPrefill?.(text) : undefined}
          />
        </Transition>
        <Transition
          as="div"
          show={activeTab === "vulnerabilities"}
          enter="transition-opacity duration-150"
          enterFrom="opacity-0"
          enterTo="opacity-100"
          leave="transition-opacity duration-100"
          leaveFrom="opacity-100"
          leaveTo="opacity-0"
        >
          <VulnTable
            items={filteredFindings}
            severityCounts={data.severityCounts}
            loading={loading}
            onCveClick={(cve, env) => onChatPrefill?.(`Handle ${cve} in ${env} environment`)}
            onEmptyAction={() => onChatPrefill?.("Scan dev for vulnerabilities")}
          />
        </Transition>
        <Transition
          as="div"
          show={activeTab === "activity"}
          enter="transition-opacity duration-150"
          enterFrom="opacity-0"
          enterTo="opacity-100"
          leave="transition-opacity duration-100"
          leaveFrom="opacity-100"
          leaveTo="opacity-0"
        >
          <ActivityTable
            items={data.activities}
            loading={loading}
            onRowClick={(cve, env) => onChatPrefill?.(`Query compliance reports for ${cve} in ${env}`)}
            onEmptyAction={() => onChatPrefill?.("Patch critical vulnerabilities in dev")}
          />
        </Transition>
        <Transition
          as="div"
          show={activeTab === "compliance"}
          enter="transition-opacity duration-150"
          enterFrom="opacity-0"
          enterTo="opacity-100"
          leave="transition-opacity duration-100"
          leaveFrom="opacity-100"
          leaveTo="opacity-0"
        >
          <ComplianceSummary
            data={data.compliance}
            reports={data.reportDetails}
            loading={loading}
            onEmptyAction={() => onChatPrefill?.("Patch critical vulnerabilities in dev")}
          />
        </Transition>
        <Transition
          as="div"
          show={activeTab === "explainability"}
          enter="transition-opacity duration-150"
          enterFrom="opacity-0"
          enterTo="opacity-100"
          leave="transition-opacity duration-100"
          leaveFrom="opacity-100"
          leaveTo="opacity-0"
        >
          <ExplainabilityTable
            items={data.reportDetails}
            loading={loading}
            onCveClick={(cve, env) => onChatPrefill?.(`Query compliance reports for ${cve} in ${env}`)}
            onEmptyAction={() => onChatPrefill?.("Patch critical vulnerabilities in dev")}
          />
        </Transition>
      </div>
    </div>
  );
}
