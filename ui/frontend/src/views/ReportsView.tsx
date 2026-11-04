import { Fragment, useEffect, useMemo, useState } from "react";
import type { DashboardData, RunRow } from "../lib/api";
import { clock, decisionText, matchesSearch, readProblems, reportResult, reportSla, reportTitle, slaDetail, tagText, whyThen, PLAN_ID_CHARS, RUN_ID_CHARS } from "../lib/view";
import { exportToCsv } from "../lib/export";
import { Callout, CopyId, Empty, Mono, State } from "./common";

const PAGE = 50;
type Kind = "all" | "patch" | "rollback" | "breached";
const KINDS: Array<[Kind, string]> = [["all", "All runs"], ["patch", "Patches"], ["rollback", "Rollbacks"], ["breached", "Breached SLA"]];

interface Props {
  data: DashboardData | null;
  loading: boolean;
  /** Search to start with: a run ID from Recent changes. */
  initialQuery?: string;
}

export default function ReportsView({ data, loading, initialQuery = "" }: Props) {
  const [kind, setKind] = useState<Kind>("all");
  const [env, setEnv] = useState("");
  const [query, setQuery] = useState(initialQuery);
  const [open, setOpen] = useState<string | null>(null);
  // Opened for one run: its details show at once.
  const [openedFor, setOpenedFor] = useState(initialQuery);
  const [shown, setShown] = useState(PAGE);
  const runs = data?.runs ?? [];
  const envs = useMemo(() => [...new Set(runs.map((r) => r.environment))].sort(), [runs]);
  const rows = useMemo(() => runs.filter((r) =>
    (kind === "all" || (kind === "breached" ? r.sla_met === false : r.operation_type === kind)) && (!env || r.environment === env)
    && matchesSearch(query, r.execution_id, r.report_id, r.plan_id, reportTitle(r), r.cve_id, r.operator)),
  [runs, kind, env, query]);

  useEffect(() => {
    if (openedFor && rows.length === 1) {
      setOpen(rows[0].report_id);
      setOpenedFor("");
    }
  }, [openedFor, rows]);

  if (!data) return loading ? <div className="d">Reading reports…</div> : <Empty title="Reports couldn't be read" />;

  const c = data.compliance;
  const unread = readProblems(data.warnings, "reports");
  const bySeverity = Object.entries(c?.by_severity ?? {}).filter(([, v]) => v.met + v.breached > 0);
  const byTag = Object.entries(c?.by_tag?.groups ?? {}).filter(([g, v]) => g !== "unknown" && v.met + v.breached > 0);
  const tagKeys = [...new Set(runs.flatMap((r) => Object.keys(r.tags ?? {})))];

  const download = () => exportToCsv("patchy-runs",
    ["When", "Change", "Environment", "Result", "Instances", "Succeeded", "Failed", "SLA", "SLA hours", "SLA source",
     "Decision", "Frameworks", ...tagKeys, "Approved by", "Execution", "Plan"],
    rows.map((r) => [r.timestamp, reportTitle(r), r.environment, reportResult(r).text, String(r.instance_count),
                     String(r.success_count), String(r.failure_count), reportSla(r).text, String(r.sla_hours ?? ""),
                     r.sla_source ?? "", decisionText(r.decision), r.frameworks.join(" "),
                     ...tagKeys.map((k) => tagText(r.tags?.[k], r.tag_values?.[k])), r.operator,
                     r.execution_id ?? r.report_id, r.plan_id ?? ""]));

  return (
    <section className="pane" aria-label="Reports">
      <h1>Reports</h1>
      <p className="lede">
        {runs.length} run{runs.length === 1 ? "" : "s"} in the last 30 days.
        {c && (c.sla_met + c.sla_breached > 0
          ? <> SLA met on {c.sla_met} of {c.sla_met + c.sla_breached} patch runs with a result.</>
          : <> No patch run has an SLA result yet.</>)}
        {c?.sla_unknown ? <> {c.sla_unknown} {c.sla_unknown === 1 ? "has" : "have"} no SLA result; a run's details say why.</> : null}
      </p>
      {bySeverity.length > 0 && (
        <p className="d mt-1">By severity: {bySeverity.map(([s, v]) => `${s.charAt(0)}${s.slice(1).toLowerCase()} ${v.met} met, ${v.breached} breached`).join("; ")}.</p>
      )}
      {byTag.length > 0 && (
        <p className="d mt-1">By {c?.by_tag?.key}: {byTag.map(([g, v]) => `${g} ${v.met} met, ${v.breached} breached`).join("; ")}.</p>
      )}
      {unread.map((w, i) => <Callout key={i} tone="risk" title={w.title}>{w.message}</Callout>)}
      {runs.length === 0 ? !unread.length && <Empty title="No runs yet">Each patch or rollback writes a report here when it ends.</Empty> : (
        <>
          <div className="filters" role="search">
            <input className="field" type="search" value={query} onChange={(e) => { setQuery(e.target.value); setShown(PAGE); }}
                   placeholder="Search by run or plan ID, CVE or approver" aria-label="Search reports" style={{ width: "22rem" }} />
          </div>
          <div className="filters" role="group" aria-label="Filter runs">
            {KINDS.map(([k, label]) => (
              <button key={k} type="button" className="chip" aria-pressed={kind === k} onClick={() => { setKind(k); setShown(PAGE); }}>{label}</button>
            ))}
            {envs.length > 1 && (
              <select className="field" aria-label="Environment" value={env} onChange={(e) => { setEnv(e.target.value); setShown(PAGE); }}>
                <option value="">All environments</option>
                {envs.map((e) => <option key={e} value={e}>{e}</option>)}
              </select>
            )}
            <span className="spacer" />
            <button type="button" className="btn" onClick={download} disabled={!rows.length}>Export CSV</button>
          </div>
          {rows.length ? (
            <table className="tbl">
              <thead><tr><th style={{ width: "100%" }}>Change</th><th>Result</th><th>SLA</th><th>When</th><th /></tr></thead>
              <tbody>
                {rows.slice(0, shown).map((r) => {
                  const res = reportResult(r), sla = reportSla(r), isOpen = open === r.report_id;
                  const how = decisionText(r.decision);
                  return (
                    <Fragment key={r.report_id}>
                      <tr className={isOpen ? "open" : ""}>
                        <td>{reportTitle(r)}<div className="sub2">{r.instance_count} instance{r.instance_count === 1 ? "" : "s"}{how ? `, ${how.toLowerCase()}` : ""}. Approved by {r.operator}
                          {r.execution_id && <>{" · "}Run <CopyId id={r.execution_id} n={RUN_ID_CHARS} label="run ID" onOpen={() => setOpen(isOpen ? null : r.report_id)}
                            openTitle={`${isOpen ? "Hide" : "Show"} the details of SSM Automation execution ${r.execution_id}`} /></>}</div></td>
                        <td className="nw"><State tone={res.tone}>{res.text}</State></td>
                        <td className="nw">{sla.tone ? <State tone={sla.tone}>{slaWords(r, sla.text)}</State> : <span className="d">{sla.text}</span>}</td>
                        <td className="nw">{clock(r.timestamp)}</td>
                        <td className="nw" style={{ textAlign: "right" }}>
                          <button type="button" className="btn quiet" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : r.report_id)}>
                            {isOpen ? "Hide" : "Details"}
                          </button>
                        </td>
                      </tr>
                      {isOpen && <tr><td colSpan={5} className="more"><Detail r={r} /></td></tr>}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          ) : <p className="d">No runs match these filters.</p>}
          {rows.length > shown && (
            <div className="pager"><span>Showing {shown} of {rows.length}</span>
              <button type="button" className="btn" onClick={() => setShown((n) => n + PAGE)}>Show {Math.min(PAGE, rows.length - shown)} more</button></div>
          )}
        </>
      )}
    </section>
  );
}

function slaWords(r: RunRow, text: string): string {
  return r.sla_hours ? `${text}, ${r.sla_hours} h SLA` : text;
}

function Detail({ r }: { r: RunRow }) {
  const rows: Array<[string, React.ReactNode]> = [];
  if (r.cve_id && r.cve_id !== "N/A") rows.push(["Vulnerability", <><Mono>{r.cve_id}</Mono>{r.severity ? `, ${r.severity.toLowerCase()}` : ""}</>]);
  if (r.install_packages.length) rows.push(["Installed", r.install_packages.join(", ")]);
  if (r.decision) rows.push(["Why then", whyThen(r)]);
  if (r.operation_type === "patch") {
    rows.push(["SLA", slaDetail(r)]);
    if (r.frameworks.length) rows.push(["Frameworks", r.frameworks.join(", ")]);
  }
  rows.push(["Outcome", `${r.success_count} succeeded, ${r.failure_count} failed${r.duration_seconds ? ` in ${Math.round(r.duration_seconds / 60)} min` : ""}`]);
  if (r.failure_message) rows.push(["Failure", r.failure_message]);
  for (const [k, v] of Object.entries(r.tags ?? {})) rows.push([k, tagText(v, r.tag_values?.[k])]);
  rows.push(["Approved by", r.operator]);
  const execution = r.execution_id ?? r.report_id;
  rows.push(["SSM Automation execution", <CopyId id={execution} n={execution.length} label="execution ID" />]);
  if (r.plan_id) rows.push(["Plan", <CopyId id={r.plan_id} n={PLAN_ID_CHARS} label="plan ID" />]);
  return (
    <dl className="grid gap-x-6 gap-y-1.5 m-0" style={{ gridTemplateColumns: "140px minmax(0, 1fr)" }}>
      {rows.map(([k, v]) => (<Fragment key={k}><dt className="d">{k}</dt><dd className="m-0">{v}</dd></Fragment>))}
    </dl>
  );
}
