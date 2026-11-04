import type { ResultCard } from "../../hooks/useAgentStream";
import { planStatus } from "../../lib/view";
import { useTick } from "../../views/common";
import type { Pane } from "../../views/pane";

/** A follow-up the user can trigger from a card. */
interface CardAction {
  label: string;
  prompt: string;
}

interface Field {
  label: string;
  value: string;
  tone?: "ok" | "warn" | "bad";
}

interface Props {
  cards: ResultCard[];
  onAction?: (prompt: string) => void;
  /** Open a plan or run in the detail pane. */
  onOpen: (pane: Pane) => void;
  /** What the detail pane shows now, to mark the matching reference. */
  current?: Pane;
}

const toneClass: Record<string, string> = { ok: "oktxt", warn: "watchtxt", bad: "risktxt" };

function asArray<T>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : [];
}

function str(v: unknown): string {
  return v === undefined || v === null ? "" : String(v);
}

function Actions({ actions, onAction }: { actions: CardAction[]; onAction?: (p: string) => void }) {
  if (!actions.length || !onAction) return null;
  return (
    <div className="acts">
      {actions.map((a, i) => (
        <button key={`${i}-${str(a.label)}`} type="button" className="btn small" onClick={() => onAction(str(a.prompt))} title={str(a.prompt)}>
          {str(a.label)}
        </button>
      ))}
    </div>
  );
}

function Fields({ fields }: { fields: Field[] }) {
  if (!fields.length) return null;
  return (
    <dl>
      {fields.map((f, i) => (
        <div key={`${i}-${str(f.label)}`} className="contents">
          <dt>{str(f.label)}</dt>
          <dd className={f.tone ? toneClass[f.tone] ?? "" : ""}>{str(f.value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function Head({ card }: { card: ResultCard }) {
  return (
    <>
      <div className="t">{str(card.title)}</div>
      {card.subtitle ? <div className="d">{str(card.subtitle)}</div> : null}
    </>
  );
}

function FleetCard({ card, onAction }: { card: ResultCard; onAction?: (p: string) => void }) {
  const rows = asArray<{ label: string; count: number; compliant?: number; note?: string }>(card.rows);
  return (
    <div className="rc">
      <Head card={card} />
      <div className="rows">
        {rows.map((r, i) => (
          <div key={`${i}-${str(r.label)}`}>
            <span>{str(r.label)}</span>
            <span className="d">{r.note !== undefined ? str(r.note) : `${str(r.count)} instances, ${str(r.compliant ?? 0)} compliant`}</span>
          </div>
        ))}
      </div>
      <Actions actions={asArray<CardAction>(card.actions)} onAction={onAction} />
    </div>
  );
}

function VulnerabilityCard({ card, onAction, onOpen }: { card: ResultCard; onAction?: (p: string) => void; onOpen: (p: Pane) => void }) {
  const counts = (card.counts ?? {}) as Record<string, number>;
  const rows = asArray<{ id: string; severity: string; summary?: string; action?: CardAction }>(card.rows);
  const tally = ["critical", "high", "medium", "low"].filter((k) => counts[k]).map((k) => `${counts[k]} ${k}`).join(", ");
  return (
    <div className="rc">
      <Head card={card} />
      {tally && <div className="d">{tally}</div>}
      <div className="rows">
        {rows.map((r, i) => (
          <div key={`${i}-${str(r.id)}`}>
            <span className="min-w-0"><span className="mono">{str(r.id)}</span> <span className="d">{str(r.severity).toLowerCase()}</span>
              {r.summary && <span className="d block">{str(r.summary)}</span>}</span>
            {r.action && onAction && (
              <button type="button" className="btn small" onClick={() => onAction(str(r.action!.prompt))} title={str(r.action.prompt)}>{str(r.action.label)}</button>
            )}
          </div>
        ))}
      </div>
      {Number(card.more) > 0 && <div className="d mt-1">and {str(card.more)} more</div>}
      <div className="acts">
        <button type="button" className="btn small" onClick={() => onOpen({ kind: "findings" })}>Open Findings</button>
        {asArray<CardAction>(card.actions).map((a, i) => onAction && (
          <button key={i} type="button" className="btn small" onClick={() => onAction(str(a.prompt))} title={str(a.prompt)}>{str(a.label)}</button>
        ))}
      </div>
    </div>
  );
}

/** A plan in the chat is a reference: the change record opens in the detail pane. */
function PlanRef({ card, onOpen, active }: { card: ResultCard; onOpen: (p: Pane) => void; active: boolean }) {
  const status = str(card.status) || "awaiting_approval";
  const now = useTick(15_000, status === "awaiting_approval");
  const s = planStatus(status, str(card.expires_at), now);
  const record = card.record as { headline?: string } | null | undefined;
  return (
    <button type="button" className={`ref ${active ? "active" : ""}`} onClick={() => onOpen({ kind: "change", planId: str(card.plan_id) })}
            aria-label={`Open the change record: ${record?.headline || str(card.title)}`}>
      <div className="t">{record?.headline || str(card.title)}</div>
      <div className="d">{s.text}</div>
    </button>
  );
}

function RunRef({ card, onOpen, active }: { card: ResultCard; onOpen: (p: Pane) => void; active: boolean }) {
  const ids = asArray<string>(card.execution_ids);
  const failed = card.status === "stop_failed" || !ids.length;
  const title = str(card.headline) || str(card.title);
  const first = asArray<Field>(card.fields)[0];
  return (
    <button type="button" className={`ref ${failed ? "bad" : ""} ${active ? "active" : ""}`}
            onClick={() => onOpen({ kind: "run", executionIds: ids, planId: str(card.plan_id) || undefined })}
            aria-label={`Open the run: ${title}`}>
      <div className="t">{title}</div>
      <div className="d">{card.status === "stopping" ? "Stop requested" : card.headline ? str(card.title).split(" · ")[0] : first ? `${first.label}: ${first.value}` : ""}</div>
    </button>
  );
}

function FieldsCard({ card, onAction }: { card: ResultCard; onAction?: (p: string) => void }) {
  const rows = asArray<{ label: string; status: string; tone?: string }>(card.rows);
  return (
    <div className="rc">
      <Head card={card} />
      <Fields fields={asArray<Field>(card.fields)} />
      {rows.length > 0 && (
        <div className="rows">
          {rows.map((r, i) => (
            <div key={`${i}-${str(r.label)}`}><span>{str(r.label)}</span><span className={r.tone ? toneClass[r.tone] ?? "d" : "d"}>{str(r.status)}</span></div>
          ))}
        </div>
      )}
      <Actions actions={asArray<CardAction>(card.actions)} onAction={onAction} />
    </div>
  );
}

export default function ResultCards({ cards, onAction, onOpen, current }: Props) {
  return (
    <div className="flex flex-col gap-2">
      {cards.map((card, i) => {
        const key = `${card.type}-${str(card.plan_id) || i}-${i}`;
        switch (card.type) {
          case "fleet":
            return <FleetCard key={key} card={card} onAction={onAction} />;
          case "vulnerabilities":
            return <VulnerabilityCard key={key} card={card} onAction={onAction} onOpen={onOpen} />;
          case "plan":
            return <PlanRef key={key} card={card} onOpen={onOpen} active={current?.kind === "change" && current.planId === card.plan_id} />;
          case "run":
            return <RunRef key={key} card={card} onOpen={onOpen}
                           active={current?.kind === "run" && current.executionIds[0] === asArray<string>(card.execution_ids)[0]} />;
          default:
            return <FieldsCard key={key} card={card} onAction={onAction} />;
        }
      })}
    </div>
  );
}
