import { useState, useMemo } from "react";
import { ArrowPathIcon, ExclamationCircleIcon, XMarkIcon } from "@heroicons/react/24/outline";
import KpiBar from "./KpiBar";
import EnvironmentCards from "./EnvironmentCards";
import VulnTable from "./VulnTable";
import ComplianceSummary from "./ComplianceSummary";
import ExplainabilityTable from "./ExplainabilityTable";
import { KpiSkeleton, CardsSkeleton } from "./ui/Skeleton";
import { useDashboard } from "../hooks/useDashboard";

interface Props {
  onChatPrefill?: (text: string) => void;
}

const TABS = [
  { id: "vulnerabilities", label: "Vulnerabilities" },
  { id: "overview", label: "Environments" },
  { id: "compliance", label: "Compliance" },
  { id: "explainability", label: "Activity & Decisions" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function DashboardPanel({ onChatPrefill }: Props) {
  const { data, loading, error, lastRefreshed, refresh } = useDashboard();
  const [activeTab, setActiveTab] = useState<TabId>("vulnerabilities");
  const [envFilter, setEnvFilter] = useState<string | null>(null);
  const [dismissedErrors, setDismissedErrors] = useState<Set<number>>(new Set());

  // Environment filter — applied to all downstream views
  const envOptions = useMemo(
    () => (data?.environments ?? []).map((e) => e.environment).sort(),
    [data?.environments]
  );

  const filteredData = useMemo(() => {
    if (!data || !envFilter) return data;
    return {
      ...data,
      environments: data.environments.filter((e) => e.environment === envFilter),
      findings: data.findings.filter((f) => (f.environments ?? [f.environment]).includes(envFilter)),
      severityCounts: data.findings
        .filter((f) => (f.environments ?? [f.environment]).includes(envFilter))
        .reduce<Record<string, number>>((acc, f) => { acc[f.severity] = (acc[f.severity] ?? 0) + 1; return acc; }, {}),
      activities: data.activities.filter((a) => a.environment === envFilter),
      reportDetails: data.reportDetails.filter((r) => r.environment === envFilter),
    };
  }, [data, envFilter]);

  if (loading && !data) {
    return (
      <div className="space-y-4 pt-1">
        <KpiSkeleton />
        <CardsSkeleton count={4} />
      </div>
    );
  }

  if (error && !data) {
    return (
      <div className="flex items-start gap-2.5 p-4 bg-red-50 border border-red-200 rounded-xl text-sm text-red-800 mt-2">
        <ExclamationCircleIcon className="w-5 h-5 text-red-500 shrink-0 mt-0.5" />
        <div>
          <p className="font-medium">Dashboard failed to load</p>
          <p className="text-xs text-red-600 mt-1">{error}</p>
          <button onClick={() => refresh(true)} className="text-xs text-blue-600 hover:text-blue-800 mt-2 transition">
            Try again
          </button>
        </div>
      </div>
    );
  }

  if (!data) return null;

  const fd = filteredData ?? data;
  const vulnCount = Object.values(fd.severityCounts).reduce((s, n) => s + n, 0);

  return (
    <div className="space-y-4">
      {/* Dashboard header with scope + environment filter */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-semibold text-gray-900">Dashboard</h2>
          {data.scopes?.[0] && (
            <span className="text-xs text-gray-400 bg-gray-100 px-2 py-0.5 rounded-full">
              {data.scopes[0].accountId} · {data.scopes[0].region}
            </span>
          )}
        </div>
        {envOptions.length > 1 && (
          <div className="flex items-center gap-1.5">
            <select
              value={envFilter ?? ""}
              onChange={(e) => setEnvFilter(e.target.value || null)}
              aria-label="Filter by environment"
              className="text-sm border border-gray-200 rounded-lg px-2.5 py-1 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent shadow-sm hover:border-gray-300 transition"
            >
              <option value="">All environments</option>
              {envOptions.map((env) => (
                <option key={env} value={env}>{env.charAt(0).toUpperCase() + env.slice(1)}</option>
              ))}
            </select>
            {envFilter && (
              <button onClick={() => setEnvFilter(null)} className="text-xs text-blue-600 hover:text-blue-800 transition">
                Clear
              </button>
            )}
          </div>
        )}
      </div>

      {/* Error alerts */}
      {data.errors.filter((_, i) => !dismissedErrors.has(i)).map((err, i) => (
        <div key={i} className="flex items-start gap-2.5 p-3 bg-red-50 border border-red-200 rounded-xl text-sm text-red-800 animate-fade-in">
          <ExclamationCircleIcon className="w-5 h-5 text-red-500 shrink-0 mt-0.5" />
          <span className="flex-1 leading-relaxed">{err}</span>
          <button onClick={() => setDismissedErrors((s) => new Set(s).add(i))} className="p-0.5 rounded-md text-red-400 hover:text-red-600 hover:bg-red-100 transition">
            <XMarkIcon className="w-4 h-4" />
          </button>
        </div>
      ))}

      {/* KPI bar */}
      <KpiBar data={fd} onTabChange={(tab) => setActiveTab(tab as TabId)} />

      {/* Tabs + refresh */}
      <div className="flex items-center justify-between border-b border-gray-200">
        <div className="flex gap-0 -mb-px">
          {TABS.map((tab) => {
            const isActive = activeTab === tab.id;
            let count: number | null = null;
            if (tab.id === "vulnerabilities") count = vulnCount;
            if (tab.id === "explainability") count = fd.reportDetails.length || fd.activities.length;
            return (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`px-4 py-2.5 text-sm font-medium border-b-2 transition whitespace-nowrap flex items-center gap-1.5 ${
                  isActive
                    ? "border-blue-600 text-blue-600"
                    : "border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300"
                }`}
              >
                {tab.label}
                {count != null && count > 0 && (
                  <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded-full min-w-[20px] text-center ${
                    isActive ? "bg-blue-100 text-blue-700" : "bg-gray-100 text-gray-600"
                  }`}>
                    {count > 999 ? `${(count / 1000).toFixed(1)}k` : count}
                  </span>
                )}
              </button>
            );
          })}
        </div>
        <div className="flex items-center gap-2 pb-1">
          {lastRefreshed && (
            <span className="text-xs text-gray-400" title={lastRefreshed.toLocaleTimeString()}>
              Updated {Math.max(1, Math.round((Date.now() - lastRefreshed.getTime()) / 60000))}m ago
            </span>
          )}
          <button
            onClick={() => refresh(true)}
            disabled={loading}
            className="p-1.5 rounded-lg hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition disabled:opacity-40"
            aria-label="Refresh"
          >
            <ArrowPathIcon className={`w-4 h-4 ${loading ? "animate-spin" : ""}`} />
          </button>
        </div>
      </div>

      {/* Tab content */}
      <div className="pt-1">
        {activeTab === "overview" && (
          <EnvironmentCards
            items={fd.environments}
            loading={loading}
            onEnvClick={(env) => onChatPrefill?.(`Show instances in ${env} environment`)}
          />
        )}
        {activeTab === "vulnerabilities" && (
          <VulnTable
            items={fd.findings}
            severityCounts={fd.severityCounts}
            loading={loading}
            onCveClick={(cve, env) => onChatPrefill?.(`Handle ${cve} in ${env} environment`)}
          />
        )}
        {activeTab === "compliance" && (
          <ComplianceSummary data={envFilter ? null : data.compliance} reports={fd.reportDetails} loading={loading} />
        )}
        {activeTab === "explainability" && (
          <ExplainabilityTable
            items={fd.reportDetails}
            loading={loading}
            onCveClick={(cve, env) => onChatPrefill?.(`Query compliance reports for ${cve} in ${env}`)}
          />
        )}
      </div>
    </div>
  );
}
