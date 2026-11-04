import { useEffect, useState } from "react";
import type { ResultCard } from "../hooks/useAgentStream";
import type { Outcome } from "../lib/channel";
import { fetchPlan, type PlanDetail, type PlanView } from "../lib/plans";
import { checkWords, clock, coverageNote, planStatus, reachGrid, resolvePlanStatus, startAgainUntil, APPROVAL_UNKNOWN } from "../lib/view";
import { CopyId, Empty, Mono, Tag, useTick } from "./common";

interface Field { label: string; value: string; tone?: string }

interface Props {
  planId: string;
  /** The plan card from this conversation, when the plan was made here. */
  card?: ResultCard;
  sessionId: string;
  canApprove: boolean;
  /** False until the server has said what the user's role is. */
  roleKnown?: boolean;
  onApprove?: (planId: string, planHash: string) => Promise<Outcome>;
  onCancel?: (planId: string) => Promise<Outcome>;
  /** Executions this plan started, from the conversation. */
  runIds?: string[];
  /** Open the run this plan started. */
  onOpenRun?: (executionIds: string[]) => void;
}

/** The change record: what the plan changes, where, when, how it stops and how it's undone, then Approve. */
export default function ChangeRecord({ planId, card, sessionId, canApprove, roleKnown = true, onApprove, onCancel,
                                      runIds = [], onOpenRun }: Props) {
  const [detail, setDetail] = useState<PlanDetail | null>(null);
  const [missing, setMissing] = useState(false);
  const [busy, setBusy] = useState<"approve" | "cancel" | null>(null);
  const [error, setError] = useState("");
  // After an approval whose reply was cut off: the operator asked to read the plan again.
  const [checks, setChecks] = useState(0);
  const cardStatus = card ? String(card.status ?? "awaiting_approval") : undefined;

  useEffect(() => {
    let cancelled = false;
    fetchPlan(planId, sessionId).then((d) => {
      if (cancelled) return;
      setDetail(d);
      setMissing(!d);
    }, () => !cancelled && setMissing(true));
    return () => { cancelled = true; };
  }, [planId, sessionId, cardStatus, checks]);

  // The card carries a short record; the full one (every target) comes from the API.
  const short = card?.record as Partial<PlanView> | null | undefined;
  const view: PlanView | null = detail?.view ?? (short?.sections ? (short as PlanView) : null);
  const status = resolvePlanStatus(cardStatus, detail, checks > 0);
  const expiresAt = String(card?.expires_at ?? detail?.expires_at ?? "");
  const planHash = String(card?.plan_hash ?? detail?.plan_hash ?? "");
  // Also while approved, so "Start it again" goes when the approval expires.
  const now = useTick(15_000, status === "awaiting_approval" || status === "approved");
  const state = planStatus(status, expiresAt, now);

  if (!view && !card) {
    if (missing) return <Empty title="This plan isn't in this conversation">Plans can be opened only from the conversation that made them.</Empty>;
    if (detail) return <Empty title="This plan has no change record">It was made before change records existed. Ask Patchy for a fresh plan.</Empty>;
    return <div className="d">Loading the plan…</div>;
  }

  const act = async (kind: "approve" | "cancel") => {
    setBusy(kind);
    setError("");
    const out = kind === "approve" ? await onApprove?.(planId, planHash) : await onCancel?.(planId);
    setBusy(null);
    if (out && !out.ok) setError(out.reason || (kind === "approve" ? "The approval didn't go through." : "The plan couldn't be cancelled."));
  };

  const ids = runIds.length ? runIds : detail?.execution_ids ?? [];
  // Approved but never started: the server lets the same approver send it again.
  const notStarted = status === "approved" && detail?.claimed === false && !ids.length;
  const againUntil = startAgainUntil(detail);
  const canStartAgain = notStarted && !!againUntil && Date.parse(againUntil) > now;
  const headline = view?.headline || short?.headline || String(card?.title ?? "Plan");
  const requested = detail?.requested_by;
  return (
    <section className="pane" aria-label="Change record">
      <div className="record-head">
        <div>
          <h1>{headline}</h1>
          <div className="meta">
            {requested && <>Requested by {requested}{detail?.created_at && <> at {clock(detail.created_at)}</>}. </>}
            Plan <CopyId id={planId} n={6} label="plan ID" />
          </div>
        </div>
        <Tag tone={notStarted ? "watch" : state.tone}>{notStarted ? "Approved, not started" : state.text}</Tag>
      </div>

      {view ? <ViewBody view={view} /> : detail && card?.fields ? <CardBody card={card} />
        : <p className="summary">{short?.summary ?? "Loading the change record…"}</p>}

      {/* Approve appears only once the full record (every target, every check) is on screen. */}
      {(state.waiting || canStartAgain) && roleKnown && canApprove && onApprove
        && !!detail && !!(view || card?.fields) && (
        <div className="approve" role="region" aria-label="Approval">
          <div className="what">
            <div>Approving runs exactly what is above, once, as you.</div>
            <div className="fine">Recorded with plan hash <Mono>{planHash.slice(0, 4)} {planHash.slice(4, 8)}</Mono>. Anything that changes the plan makes a new one.</div>
            {error && <div className="fine risktxt" role="alert">{error}</div>}
          </div>
          {onCancel && <button type="button" className="btn" disabled={!!busy} onClick={() => act("cancel")}>Cancel plan</button>}
          <button type="button" className="btn change" disabled={!!busy} onClick={() => act("approve")}>
            {busy === "approve" ? "Approving…" : notStarted ? "Start it again" : "Approve and run"}
          </button>
        </div>
      )}
      {state.waiting && roleKnown && !canApprove && (
        <div className="approve plain"><div className="what">An operator needs to approve this plan.</div></div>
      )}
      {status === "approved" && !notStarted && (
        <div className="approve plain">
          <div className="what">
            <div>Approved{detail?.decision?.by ? <> by {detail.decision.by}</> : null}{detail?.decision?.at ? <> at {clock(detail.decision.at)}</> : null}. It runs once; a new change needs a new plan.</div>
          </div>
          {onOpenRun && ids.length > 0 && <button type="button" className="btn primary" onClick={() => onOpenRun(ids)}>Follow the run</button>}
        </div>
      )}
      {status === "unknown" && (
        <div className="approve plain">
          <div className="what">{APPROVAL_UNKNOWN}</div>
          <button type="button" className="btn" onClick={() => setChecks((n) => n + 1)}>Check again</button>
        </div>
      )}
      {notStarted && !canStartAgain && (
        <div className="approve plain"><div className="what">Approved, but the run never started and the approval has expired. Ask Patchy for a fresh plan.</div></div>
      )}
    </section>
  );
}

function ViewBody({ view }: { view: PlanView }) {
  const checks = view.checks.map(checkWords);
  const warn = checks.some((c) => c.state === "warn") || (view.notes?.length ?? 0) > 0;
  return (
    <>
      <p className="summary">{view.summary}</p>
      <div className="checks">
        <h2>{warn ? "Checks" : "Checks passed"}</h2>
        <ul>
          {checks.map((c) => (
            <li key={c.key} className={c.state === "pass" ? "" : c.state}><b>{c.label}</b><span>{c.detail}</span></li>
          ))}
          {(view.notes ?? []).map((n, i) => <li key={`n${i}`} className="warn"><b>Note</b><span>{n}</span></li>)}
        </ul>
      </div>
      <dl className="record">
        {view.sections.map((s) => (
          <Section key={s.key} label={s.label}>
            <p>{s.text}</p>
            {s.key === "where" && <Reach view={view} />}
            {s.key === "when" && view.coverage?.rows.length ? <Coverage rows={view.coverage.rows} deadline={view.coverage.deadline} /> : null}
          </Section>
        ))}
      </dl>
    </>
  );
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (<><dt>{label}</dt><dd>{children}</dd></>);
}

function Reach({ view }: { view: PlanView }) {
  const grid = reachGrid(view.locations);
  const cols = grid.regions.length;
  return (
    <>
      {grid.accounts.length > 0 && cols > 0 && (
        <div className="reach" role="table" aria-label="Accounts and Regions this change reaches"
             style={{ gridTemplateColumns: `auto repeat(${cols}, minmax(0, 1fr))` }}>
          <div className="h first toprow" role="columnheader" />
          {grid.regions.map((r) => <div key={r} className="h toprow" role="columnheader">{r}</div>)}
          {grid.accounts.map((a) => (
            <Row key={a} account={a} regions={grid.regions} count={grid.count} />
          ))}
        </div>
      )}
      {view.targets.length > 0 && (
        <div className="targets-wrap">
          <table className="tbl targets">
            <thead><tr><th>Instance</th><th>Name</th><th>Account and Region</th></tr></thead>
            <tbody>
              {view.targets.map((t) => (
                <tr key={t.instance_id}>
                  <td><Mono>{t.instance_id}</Mono></td>
                  <td>{t.name || <span className="d">No Name tag</span>}
                    {t.pending_sync && <span className="d" title="Not in SSM Explorer yet: read from EC2, so no patch state"> · pending sync</span>}</td>
                  <td><Mono>{t.account}</Mono> {t.region}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {view.targets_total > view.targets.length && (
            <p className="fine mt-2">Showing the first {view.targets.length} of {view.targets_total} instances.</p>
          )}
        </div>
      )}
    </>
  );
}

/** Which targets an existing schedule patches before the deadline, by account and Region. */
function Coverage({ rows, deadline }: { rows: NonNullable<PlanView["coverage"]>["rows"]; deadline: string }) {
  const covered = rows.reduce((n, r) => n + r.covered, 0);
  const total = rows.reduce((n, r) => n + r.instances, 0);
  return (
    <div className="targets-wrap">
      <table className="tbl targets" aria-label="Schedules that patch these targets before the SLA deadline">
        <thead><tr><th>Account and Region</th><th className="num">Instances</th><th>Scheduled to patch</th>
          <th>Before the deadline</th><th>Verdict</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.account}|${r.region}`}>
              <td><Mono>{r.account}</Mono> {r.region}</td>
              <td className="num">{r.instances}</td>
              <td>{r.schedule}</td>
              <td>{r.before_deadline}</td>
              <td>{r.verdict}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="fine mt-2">{coverageNote(covered, total, deadline ? clock(deadline) : "")}</p>
    </div>
  );
}

function Row({ account, regions, count }: { account: string; regions: string[]; count: (a: string, r: string) => number | null }) {
  return (
    <>
      <div className="h first mono" role="rowheader">{account}</div>
      {regions.map((r) => {
        const n = count(account, r);
        if (n === null) return <div key={r} className="zero" role="cell">Not reached</div>;
        if (n === 0) return <div key={r} className="zero" role="cell">None tagged yet</div>;
        return <div key={r} className="hit" role="cell">{n} instance{n === 1 ? "" : "s"}</div>;
      })}
    </>
  );
}

/** A plan made before change records existed: its card fields, in the same layout. */
function CardBody({ card }: { card: ResultCard }) {
  const fields = (Array.isArray(card.fields) ? card.fields : []) as Field[];
  const warnings = (Array.isArray(card.warnings) ? card.warnings : []) as string[];
  const decision = card.decision as { text?: string } | null | undefined;
  return (
    <>
      {card.subtitle ? <p className="summary">{String(card.subtitle)}</p> : null}
      {warnings.length > 0 && (
        <div className="checks"><h2>Checks</h2><ul>
          {warnings.map((w, i) => <li key={i} className="warn"><b>Note</b><span>{w}</span></li>)}
        </ul></div>
      )}
      <dl className="record">
        {fields.map((f, i) => (
          <Section key={`${i}-${f.label}`} label={String(f.label)}>
            <p className={f.tone === "warn" ? "watchtxt" : f.tone === "bad" ? "risktxt" : ""}>{String(f.value)}</p>
          </Section>
        ))}
        {decision?.text && <Section label="When"><p>{decision.text}</p></Section>}
      </dl>
    </>
  );
}
