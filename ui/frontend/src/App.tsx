import { useState, useCallback, useEffect, useMemo, useRef } from "react";
import ChatPanel from "./components/ChatPanel";
import { CommandPalette } from "./components/CommandPalette";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { useToast, ToastContainer } from "./components/ui/Toast";
import { useAgentStream, type ResultCard } from "./hooks/useAgentStream";
import { useRole } from "./hooks/useRole";
import { useAuth } from "./hooks/useAuth";
import { useTheme } from "./hooks/useTheme";
import { useIdleLogout } from "./hooks/useIdleLogout";
import { useDashboard } from "./hooks/useDashboard";
import { isReadOnlyPrompt } from "./lib/actions";
import { holdLock, openChannel, popoutUrl, serve, type Outcome } from "./lib/channel";
import { conversationChanges, planStatus, scopeDashboard } from "./lib/view";
import TopBar from "./shell/TopBar";
import Rail from "./shell/Rail";
import { PANE_TITLES, paneFromState, paneKey, samePane, type Pane } from "./views/pane";
import OverviewView from "./views/OverviewView";
import ChangesView from "./views/ChangesView";
import ChangeRecord from "./views/ChangeRecord";
import RunView from "./views/RunView";
import FindingsView from "./views/FindingsView";
import ReportsView from "./views/ReportsView";

function cardKey(card: ResultCard): string {
  const ids = Array.isArray(card.execution_ids) ? (card.execution_ids as string[]) : [];
  return `${card.type}:${String(card.plan_id ?? "")}:${ids[0] ?? ""}:${String(card.status ?? "")}`;
}

export default function App() {
  const { role, known: roleKnown, auditor } = useRole();
  const auth = useAuth();
  const { messages, restoredCards, restoring, isStreaming, streamKind, sessionId, sendMessage, approvePlan, cancelPlan, stopRun,
          stopStreaming, clearMessages } = useAgentStream();
  const { toasts, toast, dismiss } = useToast();
  const { theme, toggle: toggleTheme } = useTheme();
  const dashboard = useDashboard();
  const refresh = dashboard.refresh;
  // Each pane is a browser history entry, so Back and Forward move between panes. The URL stays the same.
  const [pane, setPane] = useState<Pane>(() => paneFromState(window.history.state) ?? { kind: "overview" });
  const paneRef = useRef(pane);
  const open = useCallback((p: Pane) => {
    if (samePane(paneRef.current, p)) return;
    paneRef.current = p;
    setPane(p);
    window.history.pushState({ ...(window.history.state ?? {}), patchyPane: p }, "");
  }, []);
  useEffect(() => {
    window.history.replaceState({ ...(window.history.state ?? {}), patchyPane: paneRef.current }, "");
    const onPop = (e: PopStateEvent) => {
      const p = paneFromState(e.state) ?? { kind: "overview" as const };
      paneRef.current = p;
      setPane(p);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const [account, setAccount] = useState("ALL");
  const [chatPrefill, setChatPrefill] = useState("");
  const [paletteOpen, setPaletteOpen] = useState(false);
  const operator = role === "operator";

  // Auto sign-out after 60 minutes of inactivity; the ALB's session cookie lasts much longer.
  useIdleLogout(auth?.logoutUrl, 60 * 60 * 1000);

  // Changes restored after a reload come first (oldest), so a live card for the same plan or run wins.
  const changes = useMemo(() => conversationChanges([{ cards: restoredCards }, ...messages]), [restoredCards, messages]);
  const data = useMemo(() => (dashboard.data ? scopeDashboard(dashboard.data, account) : null), [dashboard.data, account]);

  const handleClear = useCallback(() => {
    clearMessages();
    open({ kind: "overview" });
    toast("New conversation started", "info");
  }, [clearMessages, toast, open]);

  // Reads from the detail views send at once (unless a reply is still streaming);
  // anything that could change something fills the chat box. Viewers can't chat.
  const handlePrompt = useCallback((text: string, note?: string) => {
    if (!operator) return;
    if (isReadOnlyPrompt(text) && !isStreaming) sendMessage(text);
    else {
      setChatPrefill(text);
      if (note) toast(note, "info");
    }
  }, [operator, isStreaming, sendMessage, toast]);

  // A plan or run that arrives in the chat opens in the detail pane (not ones restored after a reload).
  const seen = useRef<Set<string> | null>(null);
  const channel = useRef<ReturnType<typeof openChannel> | null>(null);
  useEffect(() => {
    const live = messages.filter((m) => !m.restored).flatMap((m) => m.cards ?? []);
    if (seen.current === null) {
      seen.current = new Set(live.map(cardKey));
      return;
    }
    for (const card of live) {
      const k = cardKey(card);
      if (seen.current.has(k)) continue;
      seen.current.add(k);
      if (card.type === "plan" && card.status === "awaiting_approval") {
        open({ kind: "change", planId: String(card.plan_id) });
      } else if (card.type === "run") {
        const ids = Array.isArray(card.execution_ids) ? (card.execution_ids as string[]) : [];
        if (ids.length) {
          open({ kind: "run", executionIds: ids, planId: card.plan_id ? String(card.plan_id) : undefined });
          refresh(true);   // the Changes count and Running now
        }
        if (card.plan_id) channel.current?.post({ type: "run-started", planId: String(card.plan_id), card });
      }
    }
  }, [messages, refresh, open]);

  // Tell pop-outs about every change of a plan's status, including a failed approval going back.
  const lastStatus = useRef(new Map<string, string>());
  useEffect(() => {
    for (const c of changes) {
      if (c.kind !== "plan") continue;
      const status = String(c.card.status ?? "awaiting_approval");
      if (lastStatus.current.get(c.planId) === status) continue;
      lastStatus.current.set(c.planId, status);
      channel.current?.post({ type: "plan-status", planId: c.planId, status });
    }
  }, [changes]);

  // Pop-out windows ask the main window to approve, cancel or stop, since it owns the conversation.
  // Only one main window answers: the one holding the lock, so two open tabs don't both act.
  const actions = useRef({ approvePlan, cancelPlan, stopRun });
  actions.current = { approvePlan, cancelPlan, stopRun };
  const leader = useRef(false);
  useEffect(() => {
    const release = holdLock(typeof navigator !== "undefined" ? navigator.locks : undefined, "patchy-main-window",
                             () => { leader.current = true; });
    // The pop-out's own session: a pop-out opened before New conversation still acts on its own plan.
    const stop = serve(async (m): Promise<Outcome> => {
      if (m.action === "stop") return actions.current.stopRun(m.executionId, m.sessionId);
      if (m.action === "approve") return actions.current.approvePlan(m.planId, m.planHash, m.sessionId);
      return actions.current.cancelPlan(m.planId, m.sessionId);
    }, () => leader.current);
    channel.current = openChannel(() => undefined);
    return () => { stop(); channel.current?.close(); channel.current = null; leader.current = false; release(); };
  }, []);

  // Cmd+K or Ctrl+K opens the list of things to ask. No single-key shortcuts (WCAG 2.1.4).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (operator) setPaletteOpen((v) => !v);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [operator]);

  const send = useCallback((t: string) => { sendMessage(t); }, [sendMessage]);

  const popOut = useCallback(() => {
    if (pane.kind === "change") window.open(popoutUrl("change", pane.planId), `patchy-${pane.planId}`, "popup,width=1120,height=900");
    else if (pane.kind === "run") window.open(popoutUrl("run", pane.planId ?? "", pane.executionIds), `patchy-${pane.executionIds[0]}`, "popup,width=1120,height=900");
  }, [pane]);

  const waiting = changes.filter((c) => c.kind === "plan" && planStatus(String(c.card.status ?? "awaiting_approval"), String(c.card.expires_at ?? "")).waiting).length;
  const running = dashboard.data?.runningOperations.length ?? 0;
  const planCard = (planId?: string) => changes.find((c) => c.kind === "plan" && c.planId === planId)?.card;
  const openRun = (planId: string) => (ids: string[]) => open({ kind: "run", executionIds: ids, planId });

  let body: React.ReactNode;
  switch (pane.kind) {
    case "overview":
      body = <OverviewView data={data} loading={dashboard.loading} scoped={account !== "ALL"} changes={changes}
                           canChange={operator} onOpen={open} onPrompt={handlePrompt} />;
      break;
    case "changes":
      body = <ChangesView changes={changes} data={dashboard.data} onOpen={open} auditor={auditor} />;
      break;
    case "findings":
      body = <FindingsView data={data} loading={dashboard.loading} canChange={operator} onPrompt={handlePrompt} />;
      break;
    case "reports":
      body = <ReportsView data={dashboard.data} loading={dashboard.loading} initialQuery={pane.query} />;
      break;
    case "change": {
      const run = changes.find((c) => c.kind === "run" && c.planId === pane.planId);
      body = <ChangeRecord key={pane.planId} planId={pane.planId} card={planCard(pane.planId)} sessionId={sessionId}
                           canApprove={operator} roleKnown={roleKnown} onApprove={approvePlan} onCancel={cancelPlan}
                           runIds={(run?.card.execution_ids as string[] | undefined) ?? []}
                           onOpenRun={openRun(pane.planId)} />;
      break;
    }
    case "run": {
      // The newest card for this run (a Stop request replaces it), with the run's own headline and plan.
      const cards = [...restoredCards, ...messages.flatMap((m) => m.cards ?? [])].filter((c) => c.type === "run" && (pane.executionIds[0]
        ? (c.execution_ids as string[] | undefined)?.[0] === pane.executionIds[0]
        : !(c.execution_ids as string[] | undefined)?.length && c.plan_id === pane.planId));
      const started = cards.find((c) => c.plan_id);
      const card = cards.length ? { ...cards[cards.length - 1], headline: started?.headline, plan_id: started?.plan_id,
                                    approved_by: started?.approved_by } : undefined;
      const planId = pane.planId ?? (started?.plan_id ? String(started.plan_id) : undefined);
      body = <RunView key={pane.executionIds.join(",")} executionIds={pane.executionIds} planId={planId} card={card}
                      planCard={planCard(planId)} sessionId={sessionId} canStop={operator} onStop={stopRun}
                      fromConversation={cards.length > 0}
                      onOpenPlan={planId ? () => open({ kind: "change", planId }) : undefined}
                      onOpenReports={() => open({ kind: "reports" })} />;
      break;
    }
  }

  return (
    <div className="app">
      <TopBar scopes={dashboard.data?.scopes ?? []} account={account} onAccount={setAccount} theme={theme} onTheme={toggleTheme}
              email={auth?.email ?? null} role={role} roleKnown={roleKnown} logoutUrl={auth?.logoutUrl ?? null}
              onPalette={operator ? () => setPaletteOpen(true) : undefined} />
      <Rail current={pane} onOpen={open} waiting={waiting} running={running}
            findings={data ? data.findings.length : null} reports={dashboard.data ? dashboard.data.runs.length : null}
            refreshedAt={dashboard.lastRefreshed} refreshFailed={dashboard.refreshFailed} loading={dashboard.loading} onRefresh={() => dashboard.refresh(true)} />
      <ErrorBoundary>
        <ChatPanel messages={messages} isStreaming={isStreaming} canStopReply={streamKind === "chat"} onSend={send} onStop={stopStreaming}
                   restoring={restoring} onClear={handleClear} prefillText={chatPrefill} onPrefillConsumed={() => setChatPrefill("")} role={role}
                   roleKnown={roleKnown}
                   onOpen={open} current={pane} />
      </ErrorBoundary>
      <main className="detail custom-scrollbar" aria-label="Detail">
        <div className="panebar">
          <span className="title">{PANE_TITLES[pane.kind]}</span>
          {(pane.kind === "change" || pane.kind === "run") && (
            <button type="button" className="btn quiet small" onClick={popOut} aria-label="Open in a new window">Open in new window ⧉</button>
          )}
        </div>
        <ErrorBoundary key={paneKey(pane)}>
          {body}
        </ErrorBoundary>
      </main>
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} onSend={(t) => { setPaletteOpen(false); handlePrompt(t); }} />
      <ToastContainer toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
