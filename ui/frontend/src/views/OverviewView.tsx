import { useState } from "react";
import type { DashboardData } from "../lib/api";
import { clock, matchesSearch, percentOf, planStatus, reportResult, reportTitle, PLAN_ID_CHARS, RUN_ID_CHARS, type ConversationChange } from "../lib/view";
import { Callout, CopyId, Empty, State, useTick } from "./common";
import type { Pane } from "./pane";

interface Props {
  data: DashboardData | null;
  loading: boolean;
  scoped: boolean;
  changes: ConversationChange[];
  canChange: boolean;
  onOpen: (pane: Pane) => void;
  /** Put a prompt in the chat box for the operator to send; note says what sending it does. */
  onPrompt: (text: string, note?: string) => void;
}

const SEV = ["CRITICAL", "HIGH"];

export default function OverviewView({ data, loading, scoped, changes, canChange, onOpen, onPrompt }: Props) {
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const [runQuery, setRunQuery] = useState("");
  const now = useTick(30_000);
  if (!data) return loading ? <div className="d">Reading the fleet…</div> : <Empty title="The dashboard couldn't be read">Use Refresh to try again.</Empty>;

  const envs = data.environments;
  const total = envs.reduce((s, e) => s + e.total, 0);
  const online = envs.reduce((s, e) => s + e.online, 0);
  const accounts = new Set(data.scopes.map((s) => s.accountId)).size;
  const regions = new Set(data.scopes.map((s) => s.region)).size;
  const crit = data.findingsSummary.CRITICAL;
  const scanned = envs.reduce((s, e) => s + (e.patch_compliance?.scanned_instances ?? 0), 0);
  const compliant = envs.reduce((s, e) => s + (e.patch_compliance?.compliant_instances ?? 0), 0);
  const c = data.compliance;
  const unknown = c?.sla_unknown ?? 0;
  const waiting = changes.filter((x) => x.kind === "plan" && planStatus(String(x.card.status ?? "awaiting_approval"), String(x.card.expires_at ?? ""), now).waiting);
  const unfixable = data.findings.filter((f) => f.severity === "CRITICAL" && f.fix_available !== "YES");

  // The five newest, or with a search, the five newest that match across the last 30 days.
  const recent = data.runs.filter((r) => matchesSearch(runQuery, r.execution_id, r.plan_id, reportTitle(r), r.cve_id, r.operator, r.environment)).slice(0, 5);
  return (
    <section className="pane" aria-label="Overview">
      <h1>Overview</h1>
      <p className="lede">
        {total} instance{total === 1 ? "" : "s"} in {accounts} account{accounts === 1 ? "" : "s"} and {regions} Region{regions === 1 ? "" : "s"}
        {data.runningOperations.length > 0 && <>, {data.runningOperations.length} run{data.runningOperations.length === 1 ? "" : "s"} in progress</>}.
      </p>

      {data.errors.map((e) => !dismissed.has(e) && (
        <Callout key={e} tone="risk" title="Part of the dashboard couldn't be read" onDismiss={() => setDismissed((s) => new Set(s).add(e))}>{e}</Callout>
      ))}
      {data.warnings.map((w, i) => <Callout key={`w${i}`} tone={w.tone} title={w.title}>{w.message}</Callout>)}

      <div className="kpis">
        <div className="kpi">
          <div className="label">Instances</div>
          <div className="value">{online} <small>online</small></div>
          <div className="sub">{total > online ? `${total - online} offline. ` : ""}{envs.map((e) => `${e.environment} ${e.total}`).join(", ") || "None found"}</div>
          {data.unreadableLocations.length > 0 && (
            <div className="sub watch">{data.unreadableLocations.reduce((s, u) => s + u.instances, 0)} more in locations that couldn't be read</div>
          )}
        </div>
        <div className="kpi">
          <div className="label">Critical findings</div>
          <div className="value">{crit?.ids ?? 0}</div>
          <div className="sub">
            {crit ? `${crit.instances >= 0 ? `${crit.instances} instance${crit.instances === 1 ? "" : "s"}, ` : ""}${crit.fixable_ids ? `${crit.fixable_ids} fixable by patching` : "no patch available"}` : "None open"}
          </div>
        </div>
        <div className="kpi">
          <div className="label">Patch compliance</div>
          {scanned ? <div className="value">{Math.round((compliant / scanned) * 100)}%</div> : <div className="value none">Not scanned</div>}
          <div className="sub">{scanned ? `${compliant} of ${scanned} have every approved patch` : "No patch scan results yet"}</div>
        </div>
        <div className="kpi">
          <div className="label">SLA met{scoped ? " (all accounts)" : ""}</div>
          {c?.sla_rate_percent != null ? <div className="value">{c.sla_rate_percent}%</div> : <div className="value none">No result yet</div>}
          <div className="sub">
            {c ? `${c.sla_met} met, ${c.sla_breached} breached${unknown ? `; ${unknown} run${unknown === 1 ? "" : "s"} with no result` : ""}` : "No runs in the last 30 days"}
          </div>
        </div>
      </div>

      <div className="section">
        <h2>Needs attention</h2>
        <div className="attention"><ul>
          {waiting.map((x) => (
            <li key={x.planId}>
              <span className="dot change" />
              <div><div className="t">{x.headline} is waiting for approval</div>
                <div className="d">Expires at {clock(String(x.card.expires_at ?? ""))}.</div></div>
              <button type="button" className="btn" onClick={() => onOpen({ kind: "change", planId: x.planId })}>Review</button>
            </li>
          ))}
          {data.runningOperations.length > 0 && (
            <li>
              <span className="dot change" />
              <div><div className="t">{data.runningOperations.length} run{data.runningOperations.length === 1 ? "" : "s"} in progress</div>
                <div className="d">{data.runningOperations.map((r) => `${r.operation_type === "rollback" ? "Rollback" : "Patch"} in ${r.environment}`).join(", ")}</div></div>
              <button type="button" className="btn quiet" onClick={() => onOpen({ kind: "changes" })}>Show</button>
            </li>
          )}
          {unfixable.slice(0, 3).map((f) => (
            <li key={f.cve_id}>
              <span className="dot risk" />
              <div><div className="t">{f.title || f.cve_id} on {f.instance_count} instance{f.instance_count === 1 ? "" : "s"}</div>
                <div className="d">{f.cve_id} in {f.environments.join(", ")}. No patch fixes this; see the finding for what it needs.</div></div>
              <button type="button" className="btn quiet" onClick={() => onOpen({ kind: "findings" })}>Findings</button>
            </li>
          ))}
          {data.unreadableLocations.length > 0 && (
            <li>
              <span className="dot watch" />
              <div><div className="t">{data.unreadableLocations.length} location{data.unreadableLocations.length === 1 ? "" : "s"} couldn't be read</div>
                <div className="d">{data.unreadableLocations.map((u) => `${u.account_id} ${u.region}`).join(", ")}. Their instances aren't counted above.</div></div>
              <span />
            </li>
          )}
          {!waiting.length && !data.runningOperations.length && !unfixable.length && !data.unreadableLocations.length && (
            <li><span className="dot ok" /><div className="d">Nothing needs attention.</div><span /></li>
          )}
        </ul></div>
      </div>

      <div className="section">
        <h2>Environments</h2>
        {envs.length ? (
          <div className="tblwrap"><table className="tbl">
            <thead><tr><th>Environment</th><th className="num">Instances</th><th>Patch compliance</th>
              {SEV.map((s) => <th key={s} className="num wrap">{s === "CRITICAL" ? "Critical findings" : "High findings"}</th>)}<th /></tr></thead>
            <tbody>
              {envs.map((e) => {
                const pc = e.patch_compliance;
                const pct = pc?.compliance_pct;
                return (
                  <tr key={e.environment}>
                    <td>{e.environment}</td>
                    <td className="num">{e.online === e.total ? e.total : `${e.online} of ${e.total} online`}</td>
                    <td className="nw">{pct == null ? <span className="d">Not scanned</span> : (
                      <><span className="bar"><i className={pct === 100 ? "" : pct >= 80 ? "watch" : "risk"} style={{ width: `${pct}%` }} /></span>
                        {pc?.scanned_instances ? percentOf(pc.compliant_instances, pc.scanned_instances) : `${pct}%`}</>
                    )}</td>
                    {SEV.map((s) => <td key={s} className="num">{e.vulns[s] ?? 0}</td>)}
                    {/* Wraps rather than overflowing the pane: the three buttons don't fit beside the
                        longer column names at a 1440 px window (2 Nov screenshots). */}
                    <td className="actions">
                      {canChange && <button type="button" className="btn quiet" onClick={() => onPrompt(`Show instances in ${e.environment} environment`)}>Instances</button>}
                      {canChange && <button type="button" className="btn quiet" onClick={() => onPrompt(`Preview patches for ${e.environment}`,
                          "The scan request is in the chat box. Send it to start the scan; nothing is installed.")}
                        title="Puts a dry-run scan request in the chat box. The scan starts when you send it; nothing is installed.">Scan…</button>}
                      {canChange && e.vuln_total > 0 && <button type="button" className="btn quiet" onClick={() => onPrompt(`Which findings in ${e.environment} will miss their SLA?`)}
                        title="Which findings will miss their SLA deadline">SLA risk</button>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table></div>
        ) : (
          <Empty title="No environments found">SSM Explorer can take up to 6 hours to list instances after AWS Config is enabled. The assistant reads instances directly in the meantime.</Empty>
        )}
      </div>

      <div className="section">
        <div className="section-head"><h2>Recent changes</h2>
          {data.runs.length > 0 && (
            <span className="filters" role="search">
              <input className="field" type="search" value={runQuery} onChange={(e) => setRunQuery(e.target.value)}
                     placeholder="Search by run or plan ID, CVE or approver" aria-label="Search recent changes" />
              <button type="button" className="btn quiet" onClick={() => onOpen({ kind: "reports" })}>All reports</button>
            </span>
          )}</div>
        {recent.length ? (
          <table className="tbl">
            <thead><tr><th>Change</th><th>Result</th><th>Approved by</th><th>When</th></tr></thead>
            <tbody>
              {recent.map((r) => {
                const res = reportResult(r);
                return (
                  <tr key={r.report_id}>
                    <td>{reportTitle(r)}<div className="sub2">
                      {r.execution_id && <>Run <CopyId id={r.execution_id} n={RUN_ID_CHARS} label="run ID"
                        onOpen={() => onOpen({ kind: "reports", query: r.execution_id! })} openTitle={`Open the report for SSM Automation execution ${r.execution_id}`} /></>}
                      {r.plan_id && <>{r.execution_id ? " · " : ""}Plan <CopyId id={r.plan_id} n={PLAN_ID_CHARS} label="plan ID" /></>}
                    </div></td>
                    <td><State tone={res.tone}>{res.text}</State></td>
                    <td>{r.operator}</td>
                    <td className="nw">{clock(r.timestamp)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : data.runs.length ? <p className="d">No changes in the last 30 days match “{runQuery}”.</p>
          : <p className="d">No changes in the last 30 days.</p>}
      </div>
    </section>
  );
}
