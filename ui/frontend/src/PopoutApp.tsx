import { useCallback, useEffect, useState } from "react";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { useRole } from "./hooks/useRole";
import { useAuth } from "./hooks/useAuth";
import { useTheme } from "./hooks/useTheme";
import { useIdleLogout } from "./hooks/useIdleLogout";
import type { ResultCard } from "./hooks/useAgentStream";
import { apiFetch } from "./lib/http";
import { streamAction } from "./lib/stream";
import { SESSION_KEY } from "./lib/session";
import { openChannel, request, type Outcome } from "./lib/channel";
import { approvalOutcome, statusAfterApproval, stopOutcome } from "./lib/view";
import { Brand, UserControls } from "./shell/TopBar";
import ChangeRecord from "./views/ChangeRecord";
import RunView from "./views/RunView";
import { Empty } from "./views/common";

/** The session the main window uses; a pop-out never creates or resets one. */
function storedSession(): string {
  try {
    return localStorage.getItem(SESSION_KEY) ?? "";
  } catch {
    return "";
  }
}

const requestId = () => Math.random().toString(36).slice(2) + Date.now().toString(36);
const timezone = () => Intl.DateTimeFormat().resolvedOptions().timeZone;

/** A change record or run in its own window, kept in step with the main window. */
export default function PopoutApp() {
  const params = new URLSearchParams(window.location.search);
  const planId = params.get("plan") ?? "";
  const [view, setView] = useState<"change" | "run">(params.get("pop") === "run" ? "run" : "change");
  const [executionIds, setExecutionIds] = useState<string[]>((params.get("exec") ?? "").split(",").filter(Boolean));
  const [runCard, setRunCard] = useState<ResultCard | undefined>();
  const [status, setStatus] = useState<string | undefined>();
  const { role, known } = useRole();
  const auth = useAuth();
  const { theme, toggle } = useTheme();
  // Read once: the main window may start a new conversation, but this window stays with the one it opened from.
  const [sessionId] = useState(storedSession);
  useIdleLogout(auth?.logoutUrl, 60 * 60 * 1000);

  useEffect(() => {
    const ch = openChannel((m) => {
      if (m.type === "plan-status" && m.planId === planId) setStatus(m.status);
      if (m.type === "run-started" && m.planId === planId) {
        const ids = Array.isArray(m.card.execution_ids) ? (m.card.execution_ids as string[]) : [];
        setRunCard(m.card as ResultCard);
        if (ids.length) { setExecutionIds(ids); setView("run"); }
      }
    });
    return ch.close;
  }, [planId]);

  useEffect(() => {
    document.title = view === "run" ? "Patchy run" : "Patchy change record";
  }, [view]);

  const showRun = (cards: ResultCard[]) => {
    const card = cards.find((c) => c.type === "run");
    const ids = card && Array.isArray(card.execution_ids) ? (card.execution_ids as string[]) : [];
    if (card) setRunCard(card);
    if (ids.length) { setExecutionIds(ids); setView("run"); }
  };

  // Each action goes to the main window, which owns the conversation. Only when no
  // main window acknowledges does the pop-out act on its own, with the same checks.
  const approve = useCallback(async (id: string, planHash: string): Promise<Outcome> => {
    setStatus("submitting");
    const answered = await request({ type: "request", id: requestId(), action: "approve", planId: id, planHash, sessionId });
    if (answered) {
      setStatus(statusAfterApproval(answered));
      return answered;
    }
    const out = await streamAction(`/api/plans/${id}/approve`, { session_id: sessionId, plan_hash: planHash, timezone: timezone() });
    const outcome = approvalOutcome(out.ok, out.cards, out.interrupted);
    setStatus(statusAfterApproval(outcome));
    if (outcome.ok) showRun(out.cards);
    return outcome;
  }, [sessionId]);

  const cancel = useCallback(async (id: string): Promise<Outcome> => {
    const answered = await request({ type: "request", id: requestId(), action: "cancel", planId: id, sessionId });
    if (answered) {
      if (answered.ok) setStatus("cancelled");
      return answered;
    }
    const res = await apiFetch(`/api/plans/${id}/cancel`, { method: "POST", body: JSON.stringify({ session_id: sessionId }) });
    if (res.ok) { setStatus("cancelled"); return { ok: true }; }
    if (res.status === 410) setStatus("expired");
    let reason = "The plan couldn't be cancelled.";
    try { reason = (await res.json())?.error || reason; } catch { /* not JSON */ }
    return { ok: false, reason };
  }, [sessionId]);

  const stop = useCallback(async (executionId: string): Promise<Outcome> => {
    const answered = await request({ type: "request", id: requestId(), action: "stop", executionId, sessionId });
    if (answered) return answered;
    const out = await streamAction(`/api/runs/${executionId}/stop`, { session_id: sessionId, timezone: timezone() });
    const card = out.cards.find((c) => c.type === "run");
    if (card) setRunCard((prev) => ({ ...prev, ...card, headline: prev?.headline, plan_id: prev?.plan_id } as ResultCard));
    return stopOutcome(out.ok, out.cards, out.interrupted);
  }, [sessionId]);

  // A status-only card: the record and hash come from the stored plan.
  const card = status ? ({ type: "plan", plan_id: planId, status } as ResultCard) : undefined;
  return (
    <div className="popout">
      <header className="top">
        <Brand />
        <span className="d">{view === "run" ? "Run" : "Change record"}, opened from the chat. It stays in step with the main window.</span>
        <UserControls theme={theme} onTheme={toggle} email={auth?.email ?? null} role={role} roleKnown={known} logoutUrl={null} />
      </header>
      <main className="detail custom-scrollbar">
        <ErrorBoundary>
          {!sessionId || (!planId && !executionIds.length) ? (
            <Empty title="Nothing to show">Open a change record from the chat in the main Patchy window.</Empty>
          ) : view === "run" ? (
            <RunView executionIds={executionIds} planId={planId || undefined} card={runCard} sessionId={sessionId}
                     fromConversation={!!runCard}
                     canStop={role === "operator"} onStop={stop} onOpenPlan={planId ? () => setView("change") : undefined} />
          ) : (
            <ChangeRecord planId={planId} card={card && card.status !== "awaiting_approval" ? card : undefined}
                          sessionId={sessionId} canApprove={role === "operator"} onApprove={approve} onCancel={cancel}
                          runIds={executionIds} onOpenRun={(ids) => { setExecutionIds(ids); setView("run"); }} />
          )}
        </ErrorBoundary>
      </main>
    </div>
  );
}
