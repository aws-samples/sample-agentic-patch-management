import { useEffect, useState } from "react";
import { fetchAllChanges, type AuditChange } from "../lib/plans";
import type { DashboardData } from "../lib/api";
import { auditStatus, changeRows, clock, matchesSearch, planStatus, readProblems, runState, since, PLAN_ID_CHARS, RUN_ID_CHARS, type ConversationChange } from "../lib/view";
import { Callout, CopyId, Empty, State, useTick } from "./common";
import type { Pane } from "./pane";

interface Props {
  changes: ConversationChange[];
  data: DashboardData | null;
  onOpen: (pane: Pane) => void;
  /** The user is in the auditors group: show every operator's plans, read-only. */
  auditor?: boolean;
}

function runRowState(card: Record<string, unknown>): { text: string; tone: "change" | "watch" | "risk" | "ok" | null } {
  const ids = Array.isArray(card.execution_ids) ? card.execution_ids : [];
  if (card.status === "stopping") return { text: "Stop requested", tone: "watch" };
  if (card.status === "stop_failed") return { text: "Stop failed", tone: "risk" };
  if (card.status === "finished") return { text: "Had already finished", tone: "ok" };
  if (!ids.length) return { text: "Didn't start", tone: "risk" };
  return { text: "Started; open it for progress", tone: "change" };
}

/** Plans and runs from this conversation, and every Patchy run in progress. */
export default function ChangesView({ changes, data, onOpen, auditor = false }: Props) {
  const now = useTick(30_000);
  const [query, setQuery] = useState("");
  const allRunning = data?.runningOperations ?? [];
  const runningUnread = readProblems(data?.warnings ?? [], "running");
  const running = allRunning.filter((r) => matchesSearch(query, r.execution_id, r.environment, r.operation_type));
  // One row per change: a plan reads as its run once it has one.
  const rows = changeRows(changes).map((row) => {
    const ids = (Array.isArray(row.run?.card.execution_ids) ? row.run!.card.execution_ids : []) as string[];
    const s = row.run ? runRowState(row.run.card)
      : planStatus(String(row.plan?.card.status ?? "awaiting_approval"), String(row.plan?.card.expires_at ?? ""), now);
    return { row, ids, s };
  }).filter(({ row, ids, s }) => matchesSearch(query, row.headline, row.planId, ...ids, s.text));
  return (
    <section className="pane" aria-label="Changes">
      <h1>Changes</h1>
      <p className="lede">Plans and runs from this conversation. Ask the assistant for a change; it opens here for you to review.</p>
      {(changes.length > 0 || allRunning.length > 0 || auditor) && (
        <div className="filters" role="search">
          <input className="field" type="search" value={query} onChange={(e) => setQuery(e.target.value)}
                 placeholder="Search by run or plan ID, CVE or title" aria-label="Search changes" />
        </div>
      )}
      <div className="section">
        <h2>In this conversation</h2>
        {rows.length ? (
          <table className="tbl">
            <thead><tr><th>Change</th><th>Status</th><th>When</th><th /></tr></thead>
            <tbody>
              {rows.map(({ row, ids, s }) => (
                <tr key={row.key}>
                  <td>{row.headline}<div className="sub2">
                    {row.planId && <>Plan <CopyId id={row.planId} n={PLAN_ID_CHARS} label="plan ID" /></>}
                    {row.run && <>{row.planId ? " · " : ""}{ids[0] ? <>Run <CopyId id={ids[0]} n={RUN_ID_CHARS} label="run ID" /></> : "No run"}</>}
                  </div></td>
                  <td><State tone={s.tone}>{s.text}</State></td>
                  <td className="nw">{clock(row.at)}</td>
                  <td className="nw" style={{ textAlign: "right" }}>
                    {row.run && row.planId && (
                      <button type="button" className="btn quiet" onClick={() => onOpen({ kind: "change", planId: row.planId })}>Plan</button>
                    )}
                    <button type="button" className="btn quiet" onClick={() => onOpen(row.run ? { kind: "run", executionIds: ids, planId: row.planId || undefined }
                      : { kind: "change", planId: row.planId })}>{row.run ? "Run" : "Open"}</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : changes.length ? <p className="d">No changes match “{query}”.</p>
          : <Empty title="No changes yet">Ask for a patch, a rollback or a maintenance window, for example “Patch HIGH and above in dev”.</Empty>}
      </div>
      <div className="section">
        <h2>Running now, all operators</h2>
        {runningUnread.map((w, i) => <Callout key={i} tone="risk" title={w.title}>{w.message}</Callout>)}
        {running.length ? (
          <table className="tbl">
            <thead><tr><th>Run</th><th>Environment</th><th className="num">Instances</th><th>Status</th><th>Started</th></tr></thead>
            <tbody>
              {running.map((r) => (
                <tr key={r.execution_id}>
                  <td>{r.operation_type === "rollback" ? "Rollback" : "Patch"} <CopyId id={r.execution_id} n={RUN_ID_CHARS} label="run ID" /></td>
                  <td>{r.environment}</td>
                  <td className="num">{r.instance_count || <span className="d">–</span>}</td>
                  <td><State tone={runState(r.status).tone}>{runState(r.status).text}</State></td>
                  <td className="nw">{r.started_at ? <>{clock(r.started_at)} <span className="d">({since(r.started_at, now)} ago)</span></> : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : allRunning.length ? <p className="d">No running changes match “{query}”.</p>
          : !runningUnread.length && <p className="d">No Patchy runs in progress.</p>}
      </div>
      {auditor && <AllChanges query={query} onOpen={onOpen} />}
    </section>
  );
}

/** Every operator's plans from the last week. Read-only: approving stays with each plan's conversation. */
function AllChanges({ query, onOpen }: { query: string; onOpen: (pane: Pane) => void }) {
  const [list, setList] = useState<{ changes: AuditChange[]; days: number; truncated: boolean } | null | undefined>(undefined);
  useEffect(() => {
    let live = true;
    fetchAllChanges().then((r) => live && setList(r)).catch(() => live && setList(null));
    return () => { live = false; };
  }, []);
  // Newest first by the time shown: the decision, or when the plan was made.
  const shown = (c: AuditChange) => c.decided_at || c.created_at;
  const rows = [...(list?.changes ?? [])].sort((a, b) => shown(b).localeCompare(shown(a)))
    .map((c) => ({ c, s: auditStatus(c) }))
    .filter(({ c, s }) => matchesSearch(query, c.headline, c.plan_id, c.requested_by ?? "", ...c.execution_ids, s.text));
  return (
    <div className="section">
      <h2>All operators' plans, last {list?.days ?? 7} days</h2>
      {list === undefined ? <p className="d">Loading…</p>
        : list === null ? <p className="d">The plans couldn't be loaded. Try again shortly.</p>
        : rows.length ? (
          <table className="tbl">
            <thead><tr><th>Change</th><th>Requested by</th><th>Status</th><th>When</th></tr></thead>
            <tbody>
              {rows.map(({ c, s }) => (
                <tr key={c.plan_id}>
                  <td>{c.headline}<div className="sub2">Plan <CopyId id={c.plan_id} n={PLAN_ID_CHARS} label="plan ID" />
                    {c.execution_ids[0] && <> · Run <button type="button" className="link" onClick={() => onOpen({ kind: "reports", query: c.execution_ids[0] })}>
                      {c.execution_ids[0].slice(0, RUN_ID_CHARS)}</button></>}</div></td>
                  <td>{c.requested_by ?? <span className="d">Unknown</span>}</td>
                  <td><State tone={s.tone}>{s.text}</State></td>
                  <td className="nw">{clock(shown(c))}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : list.changes.length ? <p className="d">No plans match “{query}”.</p>
          : <p className="d">No plans in the last {list.days} days.</p>}
      {list?.truncated && <p className="fine mt-2">Showing the newest {list.changes.length}.</p>}
      <p className="fine mt-2">Read-only. A plan is approved or cancelled in the conversation that made it.</p>
    </div>
  );
}
