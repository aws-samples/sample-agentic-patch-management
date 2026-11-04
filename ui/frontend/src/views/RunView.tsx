import { Fragment, useCallback, useEffect, useState } from "react";
import type { ResultCard } from "../hooks/useAgentStream";
import type { Outcome } from "../lib/channel";
import { fetchRun, type RunStatus } from "../lib/runs";
import { nextPollDelay, settled, trackRun, type RunTrack } from "../lib/runPoll";
import { fetchPlan, type PlanDetail, type PlanView } from "../lib/plans";
import { clock, overallRunState, runProgress, runState, since, STOP_TEXT } from "../lib/view";
import { ConfirmDialog } from "../components/ui/ConfirmDialog";
import { Callout, CopyId, Empty, Mono, State, Tag, useTick } from "./common";

interface Field { label: string; value: string; tone?: string }

interface Props {
  executionIds: string[];
  planId?: string;
  /** The run card from this conversation (approval outcome or stop request). */
  card?: ResultCard;
  /** The plan card, for its change record. */
  planCard?: ResultCard;
  sessionId: string;
  canStop: boolean;
  onStop?: (executionId: string) => Promise<Outcome>;
  /** The run's IDs came from this conversation's own cards. */
  fromConversation?: boolean;
  onOpenPlan?: () => void;
  onOpenReports?: () => void;
}

/** A run as it happens: progress, status by location, what has happened, and Stop. */
export default function RunView({ executionIds, planId, card, planCard, sessionId, canStop, onStop, fromConversation,
                                 onOpenPlan, onOpenReports }: Props) {
  const [runs, setRuns] = useState<Record<string, RunTrack>>({});
  const [plan, setPlan] = useState<PlanDetail | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [stopping, setStopping] = useState(card?.status === "stopping");
  const [stopError, setStopError] = useState("");
  useEffect(() => {
    if (card?.status === "stopping") setStopping(true);
    if (card?.status === "stop_failed") setStopping(false);
  }, [card?.status]);
  const key = executionIds.join(",");

  useEffect(() => {
    if (!planId) return;
    let cancelled = false;
    fetchPlan(planId, sessionId).then((d) => !cancelled && setPlan(d), () => undefined);
    return () => { cancelled = true; };
  }, [planId, sessionId]);

  useEffect(() => {
    if (!executionIds.length) return;
    let cancelled = false;
    let errorRounds = 0;
    let tracks: Record<string, RunTrack> = {};
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      // Runs already ended or gone aren't read again.
      const results = await Promise.all(executionIds.map((id) => {
        return settled(tracks[id]) ? null : fetchRun(id, sessionId);
      }));
      if (cancelled) return;
      tracks = Object.fromEntries(executionIds.map((id, i) => {
        const r = results[i];
        return [id, r ? trackRun(tracks[id], r) : tracks[id]];
      }));
      setRuns(tracks);
      errorRounds = results.some((r) => r?.kind === "error") ? errorRounds + 1 : 0;
      const delay = nextPollDelay(executionIds.map((id) => tracks[id]), errorRounds);
      if (delay !== null) timer = setTimeout(poll, delay);
    };
    poll();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, sessionId]);

  const loaded = executionIds.map((id) => runs[id]?.last).filter((r): r is RunStatus => !!r);
  const missed = executionIds.length > 0 && executionIds.every((id) => runs[id]?.gone);
  const readError = executionIds.map((id) => runs[id]?.error).find(Boolean) ?? "";
  const active = loaded.some((r) => r.active);
  // Ended in SSM, with commands it sent still going: nothing left to stop.
  const stoppable = loaded.some((r) => r.active && r.status !== "CommandsFinishing");
  // Elapsed time shows seconds for the first minute, so tick often enough for them to mean something.
  const now = useTick(5_000, active);
  // The plan card carries a short record; locations and targets come from the plan itself.
  const short = planCard?.record as Partial<PlanView> | null | undefined;
  const view: PlanView | null = plan?.view ?? (short?.locations ? (short as PlanView) : null);
  const children = loaded.flatMap((r) => r.children);
  const progress = runProgress(children, view?.locations ?? []);
  const startedAt = loaded.map((r) => r.started_at).filter(Boolean).sort()[0] ?? null;
  const endedAt = !active && loaded.length ? loaded.map((r) => r.ended_at).filter(Boolean).sort().slice(-1)[0] ?? null : null;
  const overall = overallRunState(loaded, stopping);

  const stop = useCallback(async () => {
    setConfirm(false);
    setStopping(true);
    setStopError("");
    const out = await onStop?.(executionIds[0]);
    if (out && !out.ok) {
      setStopping(false);
      setStopError(out.reason || "The stop request didn't go through.");
    }
  }, [onStop, executionIds]);

  if (!executionIds.length || (!loaded.length && missed && fromConversation)) {
    // Nothing to follow: the plan didn't start, or its run records weren't saved.
    const fields = (Array.isArray(card?.fields) ? card!.fields : []) as Field[];
    return (
      <section className="pane" aria-label="Run">
        <h1>{String(card?.headline || card?.title || "Run")}</h1>
        {!executionIds.length && <p className="lede">This plan didn't start a run.</p>}
        <dl className="record">
          {fields.map((f, i) => (
            <Fragment key={i}><dt>{f.label}</dt><dd className={f.tone === "bad" ? "risktxt" : f.tone === "warn" ? "watchtxt" : ""}>{f.value}</dd></Fragment>
          ))}
        </dl>
      </section>
    );
  }
  if (!loaded.length && missed) {
    return <Empty title="This run isn't in this conversation">Runs can be followed only from the conversation that started them.</Empty>;
  }

  const fields = (Array.isArray(card?.fields) ? card!.fields : []) as Field[];
  const problems = fields.filter((f) => ["Not started", "Stop", "Report", "Next", "Not stopped", "Reason"].includes(f.label));
  const headline = String(card?.headline || view?.headline || short?.headline || card?.title || "Run");
  const size = new Map((view?.locations ?? []).map((l) => [`${l.account}|${l.region}`, l.instances]));
  const approver = plan?.decision?.by || (card?.approved_by as string | undefined);

  return (
    <section className="pane" aria-label="Run">
      <div className="record-head">
        <div>
          <h1>{headline}</h1>
          <div className="meta">
            {approver && <>Approved by {approver}{plan?.decision?.at && <> at {clock(plan.decision.at)}</>}. </>}
            Run <CopyId id={executionIds[0]} label="run ID" />
            {executionIds.length > 1 && <> and {executionIds.length - 1} more</>}
            {onOpenPlan && <>{" · "}<button type="button" className="link" onClick={onOpenPlan}>Change record</button></>}
          </div>
        </div>
        <Tag tone={overall.tone}>{overall.text}{stoppable && !stopping && startedAt ? ` for ${since(startedAt, now)}` : ""}</Tag>
      </div>

      {progress.locations > 0 && (
        <>
          <div className="progress" role="img" aria-label={progress.text}>
            {progress.total > 0 ? (
              <>
                <i className="done" style={{ width: `${(progress.done / progress.total) * 100}%` }} />
                <i className="doing" style={{ width: `${(progress.doing / progress.total) * 100}%` }} />
                <i className="bad" style={{ width: `${(progress.failed / progress.total) * 100}%` }} />
              </>
            ) : (
              <i className="doing" style={{ width: `${(progress.locationsDone / progress.locations) * 100}%` }} />
            )}
          </div>
          <p className="lede">{progress.text}</p>
        </>
      )}
      {readError && <Callout title="Status not updated">{readError} Showing the last status read; trying again.</Callout>}
      {problems.map((f, i) => (
        <Callout key={i} tone={f.tone === "bad" ? "risk" : "watch"} title={f.label}>{f.value}</Callout>
      ))}

      <div className="section">
        <h2>By location</h2>
        {children.length ? (
          <table className="tbl">
            <thead><tr><th>Account and Region</th><th className="num">Instances</th><th>Status</th><th>Since</th></tr></thead>
            <tbody>
              {children.map((c, i) => {
                const s = runState(c.status);
                const n = size.get(`${c.account}|${c.region}`);
                return (
                  <tr key={`${c.account}-${c.region}-${i}`}>
                    <td><Mono>{c.account}</Mono> {c.region}</td>
                    <td className="num">{n ?? <span className="d">–</span>}</td>
                    <td><State tone={s.tone}>{c.status === "Success" ? "Finished" : s.text}</State></td>
                    <td className="nw">{clock(c.ended_at || c.started_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : <p className="d">Waiting for the first location to start.</p>}
      </div>

      <div className="section">
        <h2>What has happened</h2>
        <ul className="timeline">
          {timeline(loaded, children, approver, plan?.decision?.at ?? null, stopping, endedAt, overall.text).map((e, i) => (
            <li key={i} className={e.tone ?? ""}><div>{e.text}</div>{e.at && <div className="when">{clock(e.at)}</div>}</li>
          ))}
        </ul>
      </div>

      {stoppable && canStop && onStop && !stopping && (
        <div className="approve plain">
          <div className="what">
            <div>{STOP_TEXT.offer}</div>
            <div className="fine">A compliance report is written when the run ends.</div>
            {stopError && <div className="fine risktxt" role="alert">{stopError}</div>}
          </div>
          <button type="button" className="btn stop" onClick={() => setConfirm(true)}>Stop run</button>
        </div>
      )}
      {stopping && active && <div className="approve plain"><div className="what">{STOP_TEXT.requested}</div></div>}
      {card?.status === "finished" && <Callout title="Nothing to stop">Every location had already finished.</Callout>}
      {!active && loaded.length > 0 && (
        <div className="approve plain">
          <div className="what">The run has ended. Its compliance report appears in Reports within about 5 minutes.</div>
          {onOpenReports && <button type="button" className="btn" onClick={onOpenReports}>Open Reports</button>}
        </div>
      )}
      <ConfirmDialog
        open={confirm}
        title="Stop this run?"
        description={STOP_TEXT.confirm}
        confirmLabel="Stop run"
        cancelLabel="Keep running"
        variant="destructive"
        onConfirm={stop}
        onCancel={() => setConfirm(false)}
      />
    </section>
  );
}

function timeline(runs: RunStatus[], children: RunStatus["children"], approver: string | undefined, approvedAt: string | null,
                  stopping: boolean, endedAt: string | null, outcome: string) {
  const events: Array<{ text: string; at: string | null; tone?: string }> = [];
  if (approver || approvedAt) events.push({ text: `Approved${approver ? ` by ${approver}` : ""}`, at: approvedAt });
  const started = runs.map((r) => r.started_at).filter(Boolean).sort()[0];
  if (started) events.push({ text: `Started in ${children.length || runs.length} location${(children.length || runs.length) === 1 ? "" : "s"}`, at: started });
  for (const c of children) {
    const s = runState(c.status);
    if (!s.active && c.ended_at) {
      events.push({ text: `${c.status === "Success" ? "Finished" : s.text} in ${c.account} ${c.region}`, at: c.ended_at,
                    tone: s.tone === "ok" ? "ok" : s.tone === "risk" ? "risk" : undefined });
    } else if (c.started_at && s.active) {
      events.push({ text: `${s.text} in ${c.account} ${c.region}`, at: c.started_at, tone: "change" });
    }
  }
  if (stopping) events.push({ text: "Stop requested", at: null, tone: "change" });
  if (endedAt) events.push({ text: `Run ${outcome.toLowerCase()}`, at: endedAt });
  return events.sort((a, b) => (b.at ?? "9").localeCompare(a.at ?? "9"));
}
