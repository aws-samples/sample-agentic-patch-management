import { useMemo, useState } from "react";
import type { DashboardData, VulnerabilityFinding } from "../lib/api";
import { filterFindings, findingPrompts, noFindingsText, readProblems } from "../lib/view";
import { exportToCsv } from "../lib/export";
import { Callout, Empty, Mono, State } from "./common";

const PAGE = 50;
const SEVERITIES = ["ALL", "CRITICAL", "HIGH", "MEDIUM", "LOW"];
const SEV_TONE: Record<string, "risk" | "watch" | null> = { CRITICAL: "risk", HIGH: "watch" };

interface Props {
  data: DashboardData | null;
  loading: boolean;
  canChange: boolean;
  onPrompt: (text: string) => void;
}

function sentence(word: string): string {
  return word.charAt(0) + word.slice(1).toLowerCase();
}

/** Inspector titles often repeat the ID ("CVE-2026-1 - gawk"); show what follows it. */
export function findingTitle(f: Pick<VulnerabilityFinding, "cve_id" | "title">): string {
  const title = (f.title ?? "").trim();
  if (f.cve_id && title.startsWith(f.cve_id)) return title.slice(f.cve_id.length).replace(/^[\s:–-]+/, "");
  return title;
}

function where(f: VulnerabilityFinding): string {
  const envs = (f.environments?.length ? f.environments : [f.environment]).join(", ");
  const accounts = f.accounts?.length ?? 0;
  return `${f.instance_count} instance${f.instance_count === 1 ? "" : "s"} in ${envs}${accounts > 1 ? `, ${accounts} accounts` : ""}`;
}

export default function FindingsView({ data, loading, canChange, onPrompt }: Props) {
  const [severity, setSeverity] = useState("ALL");
  const [fixable, setFixable] = useState(false);
  const [query, setQuery] = useState("");
  const [shown, setShown] = useState(PAGE);
  const findings = data?.findings ?? [];
  const rows = useMemo(() => filterFindings(findings, { severity, fixable, query }), [findings, severity, fixable, query]);

  if (!data) return loading ? <div className="d">Reading findings…</div> : <Empty title="Findings couldn't be read" />;
  // PARTIAL: a patch fixes some of the affected packages, so patching is still worth planning.
  const fixableCount = findings.filter((f) => f.fix_available === "YES" || f.fix_available === "PARTIAL").length;
  const unread = readProblems(data.warnings, "findings");

  const download = () => exportToCsv("findings",
    ["Finding", "Title", "Severity", "CVSS", "Fix available", "Instances", "Environments", "Accounts", "Regions"],
    rows.map((f) => [f.cve_id, f.title, f.severity, String(f.cvss_score ?? ""), f.fix_available, String(f.instance_count),
                     (f.environments ?? []).join(" "), (f.accounts ?? []).join(" "), (f.regions ?? []).join(" ")]));

  return (
    <section className="pane" aria-label="Findings">
      <h1>Findings</h1>
      <p className="lede">
        {findings.length ? <>{findings.length} vulnerabilit{findings.length === 1 ? "y" : "ies"} from Amazon Inspector. {fixableCount} can be fixed by patching; {findings.length - fixableCount} can't.</>
          : noFindingsText(unread.length > 0)}
      </p>
      {unread.map((w, i) => <Callout key={i} tone="risk" title={w.title}>{w.message}</Callout>)}
      {findings.length > 0 && (
        <>
          <div className="filters" role="group" aria-label="Filter findings">
            {SEVERITIES.map((s) => (
              <button key={s} type="button" className="chip" aria-pressed={severity === s} onClick={() => { setSeverity(s); setShown(PAGE); }}>
                {s === "ALL" ? "All severities" : sentence(s)}
              </button>
            ))}
            <button type="button" className="chip" aria-pressed={fixable} onClick={() => { setFixable((v) => !v); setShown(PAGE); }}>Fixable only</button>
            <input className="field" type="search" placeholder="Search by ID or title" aria-label="Search findings"
                   value={query} onChange={(e) => { setQuery(e.target.value); setShown(PAGE); }} />
            <span className="spacer" />
            <button type="button" className="btn" onClick={download} disabled={!rows.length}>Export CSV</button>
          </div>
          {rows.length ? (
            <table className="tbl">
              <thead><tr><th style={{ width: "100%" }}>Vulnerability</th><th>Severity</th><th className="num">CVSS</th><th /></tr></thead>
              <tbody>
                {rows.slice(0, shown).map((f) => {
                  // Plan patch starts in the lowest environment the finding affects.
                  const prompt = findingPrompts(f);
                  return (
                    <tr key={f.cve_id}>
                      <td>
                        {f.cve_id && f.cve_id !== "N/A" && <><Mono>{f.cve_id}</Mono> </>}{findingTitle(f) || (f.cve_id === "N/A" ? "Untitled finding" : "")}
                        <div className="sub2">{f.fix_available === "YES" ? "A patch fixes this." : f.fix_available === "PARTIAL" ? "A patch fixes part of this." : "No patch fixes this."} {where(f)}</div>
                      </td>
                      <td className="nw"><State tone={SEV_TONE[f.severity] ?? null}>{sentence(f.severity)}</State></td>
                      <td className="num">{f.cvss_score ?? <span className="d">–</span>}</td>
                      <td className="nw" style={{ textAlign: "right" }}>
                        {canChange && (f.fix_available === "YES" || f.fix_available === "PARTIAL") && (
                          <button type="button" className="btn quiet" onClick={() => onPrompt(prompt.plan)}>Plan patch</button>
                        )}
                        {canChange && (
                          <button type="button" className="btn quiet" onClick={() => onPrompt(prompt.instances)}>Instances</button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : <p className="d">No findings match these filters.</p>}
          {rows.length > shown && (
            <div className="pager"><span>Showing {shown} of {rows.length}</span>
              <button type="button" className="btn" onClick={() => setShown((n) => n + PAGE)}>Show {Math.min(PAGE, rows.length - shown)} more</button></div>
          )}
        </>
      )}
    </section>
  );
}
