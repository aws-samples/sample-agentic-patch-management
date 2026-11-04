import { useState, useCallback, useRef, useEffect } from "react";
import { apiFetch, retryable, SignInExpired } from "../lib/http";
import { SseParser } from "../lib/sse";
import { approvalOutcome, cardsFromPlans, statusAfterApproval, stopOutcome } from "../lib/view";
import { fetchPlan } from "../lib/plans";
import { streamAction } from "../lib/stream";
import type { Outcome } from "../lib/channel";
import { plansKey, SESSION_KEY, SESSION_USER_KEY } from "../lib/session";

/** A typed result card streamed from a tool's display block. */
export interface ResultCard {
  type: string;
  [key: string]: unknown;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  /** Restored after a reload from AgentCore Memory; not a live reply. */
  restored?: boolean;
  content: string;
  tools: string[];
  cards?: ResultCard[];
  durationMs?: number;
  error?: string;
  timestamp: number;
}

/** What is streaming: a chat reply, or an approve or stop action (which Stop reply mustn't cut off). */
export type StreamKind = "chat" | "approve" | "stop";

interface StreamState {
  messages: ChatMessage[];
  /** Plans and runs of a conversation restored after a reload. They go to Changes, not into the chat. */
  restoredCards: ResultCard[];
  /** The chat history is still loading. */
  restoring: boolean;
  isStreaming: boolean;
  streamKind: StreamKind | null;
  sessionId: string;
}

type Target = { url: string; body: Record<string, unknown>; retry?: boolean };
/** accepted: the server took the request. interrupted: it may have been carried out, but the reply didn't finish. */
type RunResult = { accepted: boolean; cards: ResultCard[]; interrupted: boolean };

// ── Session persistence ────────────────────────────────────────────
// Persist sessionId AND the user's email to localStorage so we can:
//   1. Rehydrate the chat panel on refresh from AgentCore Memory.
//   2. Detect "different user signs in" and start fresh.
//
// AgentCore Memory is the source of truth for messages. localStorage
// only holds the keys (session_id, email) — never the messages.

function uuid(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  // Fallback for environments where crypto.randomUUID is unavailable
  // (older browsers, non-HTTPS localhost, some WebViews).
  const bytes = new Uint8Array(16);
  if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") {
    crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 1
  const h = Array.from(bytes, (b) => b.toString(16).padStart(2, "0"));
  return `${h.slice(0, 4).join("")}-${h.slice(4, 6).join("")}-${h.slice(6, 8).join("")}-${h.slice(8, 10).join("")}-${h.slice(10).join("")}`;
}

function newSessionId(): string {
  return `web-${uuid().replace(/-/g, "")}`;
}

function getOrCreateSessionId(): string {
  try {
    const stored = localStorage.getItem(SESSION_KEY);
    if (stored) return stored;
  } catch {
    // localStorage unavailable (private browsing, etc.)
  }
  const id = newSessionId();
  try {
    localStorage.setItem(SESSION_KEY, id);
  } catch {
    // best-effort
  }
  return id;
}

function resetSessionId(): string {
  const id = newSessionId();
  try {
    // The old conversation's plan list would otherwise stay behind for good.
    const old = localStorage.getItem(SESSION_KEY);
    if (old) localStorage.removeItem(plansKey(old));
    localStorage.setItem(SESSION_KEY, id);
    localStorage.removeItem(SESSION_USER_KEY);
  } catch {
    // best-effort
  }
  return id;
}

// Plan IDs this conversation made, so its plans and runs come back after a reload
// (the restored chat history has text only). Newest last, at most 20.

function storedPlanIds(sid: string): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(plansKey(sid)) || "[]");
    return Array.isArray(v) ? v.filter((x) => typeof x === "string") : [];
  } catch {
    return [];
  }
}

function rememberPlan(sid: string, planId: string): void {
  try {
    const ids = storedPlanIds(sid).filter((x) => x !== planId);
    localStorage.setItem(plansKey(sid), JSON.stringify([...ids, planId].slice(-20)));
  } catch {
    // best-effort
  }
}

/** Plan and run cards rebuilt from the stored plans, for a conversation restored after a reload. */
async function readRestoredCards(sid: string): Promise<ResultCard[]> {
  const details = await Promise.all(storedPlanIds(sid).map((id) => fetchPlan(id, sid).catch(() => null)));
  return cardsFromPlans(details);
}

function getStoredUser(): string | null {
  try {
    return localStorage.getItem(SESSION_USER_KEY);
  } catch {
    return null;
  }
}

function setStoredUser(email: string): void {
  try {
    localStorage.setItem(SESSION_USER_KEY, email);
  } catch {
    // best-effort
  }
}

/**
 * Hook for streaming chat with the patch automation agent via SSE.
 *
 * Session ID is persisted to localStorage so conversation context
 * survives page refreshes. AgentCore Memory stores the full conversation
 * server-side; on mount we fetch the last K turns and rehydrate the
 * chat panel. Sign-out clears the keys; signing in as a different user
 * is detected by comparing the stored email to /api/auth/config.
 */
export function useAgentStream() {
  const [state, setState] = useState<StreamState>({
    messages: [],
    restoredCards: [],
    restoring: true,
    isStreaming: false,
    streamKind: null,
    sessionId: getOrCreateSessionId(),
  });

  const abortRef = useRef<AbortController | null>(null);
  // One stream at a time, checked synchronously (state updates land a render late).
  const streamingRef = useRef(false);
  const kindRef = useRef<StreamKind | null>(null);
  // Set once the user sends anything, so a slow rehydration can't wipe it.
  const sentRef = useRef(false);

  // ── Rehydrate from AgentCore Memory on mount ──────────────────────
  // 1. Get current Cognito email from /api/auth/config.
  // 2. If stored email differs from current → mint fresh session,
  //    don't try to rehydrate (different user).
  // 3. Else fetch /api/session/<id>/messages and replace state.messages.
  // 4. On 404 / empty → mint fresh session (memory wiped or new user).
  useEffect(() => {
    let cancelled = false;
    const finished = () => { if (!cancelled) setState((prev) => (prev.restoring ? { ...prev, restoring: false } : prev)); };
    (async () => {
      // Best-effort fetch of current user identity.
      let currentEmail: string | null = null;
      try {
        const authRes = await apiFetch("/api/auth/config");
        if (authRes.ok) {
          const data = await authRes.json();
          currentEmail = data?.email ?? null;
        }
      } catch {
        // ignore — proceed without identity check
      }

      const storedEmail = getStoredUser();

      // Different user → fresh session, no rehydrate.
      if (storedEmail && currentEmail && storedEmail !== currentEmail) {
        if (cancelled || sentRef.current) return;
        setState((prev) => ({
          ...prev,
          sessionId: resetSessionId(),
          messages: [],
        }));
        return;
      }

      // Same (or unknown) user → try to rehydrate the existing session.
      const sid = state.sessionId;
      try {
        const res = await apiFetch(`/api/session/${sid}/messages`);
        if (cancelled || sentRef.current) return; // the user has started typing to this session
        if (res.status === 200) {
          const data = await res.json();
          if (Array.isArray(data?.messages) && data.messages.length > 0) {
            const restored: ChatMessage[] = data.messages.map((m: { role: string; content: string }) => ({
              id: uuid(),
              role: m.role === "assistant" ? "assistant" : "user",
              content: m.content,
              tools: [],
              timestamp: Date.now(),
              restored: true,
            }));
            setState((prev) => ({ ...prev, messages: restored, restoring: false }));
            // The plans and runs come back into Changes once they're read.
            const cards = await readRestoredCards(sid);
            if (cancelled || !cards.length) return;
            setState((prev) => (prev.sessionId === sid ? { ...prev, restoredCards: cards } : prev));
          }
        } else if (res.status === 404) {
          // Memory wiped (agent redeployed) or session never existed.
          // Mint fresh and clear so the operator gets a clean slate.
          setState((prev) => ({
            ...prev,
            sessionId: resetSessionId(),
            messages: [],
          }));
        }
        // On 503 (memory not configured) or other errors: leave state
        // alone — empty chat with the fresh session ID.
      } catch (err) {
        console.warn("[rehydration] Failed to load session history:", err);
      }
    })().finally(finished);
    return () => {
      cancelled = true;
    };
    // Run once on mount only — re-running on sessionId change would
    // overwrite the freshly minted session before the user types.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Send a prompt (or an approve/stop request) and stream the reply. Resolves to whether it was accepted, and its cards. */
  const run = useCallback(
    async (text: string, target?: Target, kind: StreamKind = "chat"): Promise<RunResult> => {
      if (streamingRef.current) return { accepted: false, cards: [], interrupted: false };
      const cards: ResultCard[] = [];
      streamingRef.current = true;
      kindRef.current = kind;
      sentRef.current = true;
      const userMsg: ChatMessage = {
        id: uuid(),
        role: "user",
        content: text,
        tools: [],
        timestamp: Date.now(),
      };

      const assistantMsg: ChatMessage = {
        id: uuid(),
        role: "assistant",
        content: "",
        tools: [],
        timestamp: Date.now(),
      };

      setState((prev) => ({
        ...prev,
        messages: [...prev.messages, userMsg, assistantMsg],
        isStreaming: true,
        streamKind: kind,
      }));

      // Stamp the current user on first send so the next page load can
      // detect a user switch (different Cognito email → fresh session).
      // Best-effort — failure here doesn't block the message.
      try {
        const authRes = await apiFetch("/api/auth/config");
        if (authRes.ok) {
          const data = await authRes.json();
          if (data?.email) setStoredUser(data.email);
        }
      } catch {
        // ignore
      }

      const assistantId = assistantMsg.id;
      abortRef.current = new AbortController();
      let accepted = false, responded = false, interrupted = false;
      let gotDone = false, started = false, cut = false;
      let renderedContent = ""; // What's already displayed
      let contentBuffer = "";   // Paragraph-level buffer not yet displayed

      try {
        const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
        const request = target ?? {
          url: "/api/chat",
          body: { message: text, session_id: state.sessionId, timezone },
          retry: true,
        };
        const fetchWithRetry = async (retries = request.retry === false ? 0 : 2): Promise<Response> => {
          const res = await apiFetch(request.url, {
            method: "POST",
            body: JSON.stringify(request.body),
            signal: abortRef.current?.signal,
          });
          // Only "starting up" is safe to resend; any other 5xx may have reached the agent.
          if (retryable(res.status) && retries > 0) {
            await new Promise((r) => setTimeout(r, 1000));
            return fetchWithRetry(retries - 1);
          }
          return res;
        };

        const res = await fetchWithRetry();
        responded = true;

        if (!res.ok || !res.body) {
          let serverError = "";
          try {
            serverError = (await res.clone().json())?.error ?? "";
          } catch {
            // not JSON
          }
          const detail = serverError ? serverError
            : res.status === 503 ? "Agent is starting up. Try again in a few seconds."
            : res.status >= 500 ? "Server error. The agent may be restarting."
            : `Request failed (${res.status}).`;
          throw new Error(detail);
        }
        accepted = true;

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        const parser = new SseParser();

        // Flush complete blocks (paragraphs, tables) to the UI
        const flushBlocks = () => {
          // Find the last block boundary (double newline)
          const lastBoundary = contentBuffer.lastIndexOf("\n\n");
          if (lastBoundary === -1) return; // No complete block yet

          const completeBlocks = contentBuffer.slice(0, lastBoundary + 2);
          contentBuffer = contentBuffer.slice(lastBoundary + 2);
          renderedContent += completeBlocks;

          setState((prev) => ({
            ...prev,
            messages: prev.messages.map((m) =>
              m.id === assistantId
                ? { ...m, content: renderedContent }
                : m
            ),
          }));
        };

        const handle = (raw: unknown) => {
          const event = raw as { type?: string; content?: string; tool?: string; card?: ResultCard;
                                 duration_ms?: number; message?: string };
          if (event.type === "done") gotDone = true;
          else if (event.type === "error") cut ||= started;
          else started = true;
          if (event.type === "text") {
            contentBuffer += event.content ?? "";
            flushBlocks(); // Render any complete blocks
          } else if (event.type === "tool_start" && event.tool) {
            // Raw tool name; MessageBubble shows it in plain words.
            const label = event.tool;
            setState((prev) => ({
              ...prev,
              messages: prev.messages.map((m) =>
                m.id === assistantId && !m.tools.includes(label) ? { ...m, tools: [...m.tools, label] } : m
              ),
            }));
          } else if (event.type === "card" && event.card && typeof event.card.type === "string") {
            const card = event.card;
            cards.push(card);
            if (card.type === "plan" && typeof card.plan_id === "string") rememberPlan(state.sessionId, card.plan_id);
            setState((prev) => ({
              ...prev,
              messages: prev.messages.map((m) =>
                m.id === assistantId ? { ...m, cards: [...(m.cards ?? []), card] } : m
              ),
            }));
          } else if (event.type === "done") {
            renderedContent += contentBuffer;
            contentBuffer = "";
            setState((prev) => ({
              ...prev,
              messages: prev.messages.map((m) =>
                m.id === assistantId ? { ...m, content: renderedContent, durationMs: event.duration_ms } : m
              ),
            }));
          } else if (event.type === "error") {
            setState((prev) => ({
              ...prev,
              messages: prev.messages.map((m) => (m.id === assistantId ? { ...m, error: event.message } : m)),
            }));
          }
        };

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          parser.push(decoder.decode(value, { stream: true })).forEach(handle);
        }
        parser.push(decoder.decode()).forEach(handle);
        parser.end().forEach(handle);
        interrupted = !gotDone || cut;
      } catch (err: unknown) {
        // Cut off after the server took it, or lost before an answer came: it may have been carried out.
        interrupted = accepted || (!responded && !(err instanceof SignInExpired));
        if (!(err instanceof DOMException && err.name === "AbortError")) {
          const message = err instanceof Error ? err.message : "Unknown error";
          setState((prev) => ({
            ...prev,
            messages: prev.messages.map((m) => (m.id === assistantId ? { ...m, error: message } : m)),
          }));
        }
      } finally {
        // Stopped or cut off: keep the text that had arrived but wasn't shown yet.
        if (contentBuffer) {
          renderedContent += contentBuffer;
          contentBuffer = "";
          const content = renderedContent;
          setState((prev) => ({
            ...prev,
            messages: prev.messages.map((m) => (m.id === assistantId ? { ...m, content } : m)),
          }));
        }
        streamingRef.current = false;
        kindRef.current = null;
        setState((prev) => ({ ...prev, isStreaming: false, streamKind: null }));
        abortRef.current = null;
      }
      return { accepted, cards, interrupted };
    },
    [state.sessionId]
  );

  const sendMessage = useCallback(
    async (text: string, target?: Target) => (await run(text, target)).accepted,
    [run]
  );

  /**
   * For approve and stop: end a chat reply that is still streaming first, so the action isn't refused.
   * Another approve or stop is never cut off (that would lose its outcome); wait for it instead.
   */
  const runAction = useCallback(
    async (text: string, target: Target, kind: StreamKind) => {
      if (streamingRef.current && kindRef.current === "chat") {
        abortRef.current?.abort();
        for (let i = 0; i < 50 && streamingRef.current; i++) await new Promise((r) => setTimeout(r, 100));
      }
      for (let i = 0; i < 600 && streamingRef.current; i++) await new Promise((r) => setTimeout(r, 100));
      return run(text, target, kind);
    },
    [run]
  );

  // Stop reply ends chat replies only.
  const stopStreaming = useCallback(() => {
    if (kindRef.current === "chat") abortRef.current?.abort();
  }, []);

  const clearMessages = useCallback(() => {
    try { localStorage.removeItem(plansKey(state.sessionId)); } catch { /* best-effort */ }
    setState((prev) => ({
      ...prev,
      messages: [],
      restoredCards: [],
      sessionId: resetSessionId(),
    }));
  }, [state.sessionId]);

  const setPlanStatus = useCallback((planId: string, status: string) => {
    const set = (c: ResultCard) => (c.type === "plan" && c.plan_id === planId ? { ...c, status } : c);
    setState((prev) => ({
      ...prev,
      messages: prev.messages.map((m) =>
        m.cards?.some((c) => c.type === "plan" && c.plan_id === planId) ? { ...m, cards: m.cards.map(set) } : m
      ),
      restoredCards: prev.restoredCards.some((c) => c.type === "plan" && c.plan_id === planId)
        ? prev.restoredCards.map(set) : prev.restoredCards,
    }));
  }, []);

  // Approving streams the run's outcome as a new assistant message. The plan reads
  // "Submitting" until the run card arrives; it's approved only if a run started.
  // A pop-out from another conversation (one this window has since replaced) asks
  // with its own session; that action happens outside this window's chat.
  const approvePlan = useCallback(
    async (planId: string, planHash?: string, sessionId = state.sessionId): Promise<Outcome> => {
      const body = { session_id: sessionId, plan_hash: planHash, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone };
      if (sessionId !== state.sessionId) {
        const r = await streamAction(`/api/plans/${planId}/approve`, body);
        return approvalOutcome(r.ok, r.cards, r.interrupted);
      }
      // The approval names the plan the operator saw; the server refuses it if the plan changed.
      const card = [...state.restoredCards, ...state.messages.flatMap((m) => m.cards ?? [])]
        .find((c) => c.type === "plan" && c.plan_id === planId);
      setPlanStatus(planId, "submitting");
      const { accepted, cards, interrupted } = await runAction(`Approve plan ${planId.slice(0, 6)}`, {
        url: `/api/plans/${planId}/approve`,
        body: { ...body, plan_hash: planHash ?? card?.plan_hash },
        retry: false,
      }, "approve");
      const out = approvalOutcome(accepted, cards, interrupted);
      setPlanStatus(planId, statusAfterApproval(out));
      return out;
    },
    [runAction, setPlanStatus, state.sessionId, state.messages, state.restoredCards]
  );

  const cancelPlan = useCallback(
    async (planId: string, sessionId = state.sessionId): Promise<Outcome> => {
      const res = await apiFetch(`/api/plans/${planId}/cancel`, {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId }),
      });
      if (res.ok) {
        setPlanStatus(planId, "cancelled");
        return { ok: true };
      }
      if (res.status === 410) setPlanStatus(planId, "expired");
      let reason = "The plan couldn't be cancelled.";
      try { reason = (await res.json())?.error || reason; } catch { /* not JSON */ }
      return { ok: false, reason };
    },
    [setPlanStatus, state.sessionId]
  );

  // Stop goes through the runtime like Approve; its card streams into the chat.
  const stopRun = useCallback(
    async (executionId: string, sessionId = state.sessionId): Promise<Outcome> => {
      const target = {
        url: `/api/runs/${executionId}/stop`,
        body: { session_id: sessionId, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone },
        retry: false,
      };
      if (sessionId !== state.sessionId) {
        const r = await streamAction(target.url, target.body);
        return stopOutcome(r.ok, r.cards, r.interrupted);
      }
      const { accepted, cards, interrupted } = await runAction(`Stop run ${executionId.slice(0, 8)}`, target, "stop");
      return stopOutcome(accepted, cards, interrupted);
    },
    [runAction, state.sessionId]
  );

  return {
    messages: state.messages,
    restoredCards: state.restoredCards,
    restoring: state.restoring,
    isStreaming: state.isStreaming,
    streamKind: state.streamKind,
    sessionId: state.sessionId,
    sendMessage,
    approvePlan,
    cancelPlan,
    stopRun,
    stopStreaming,
    clearMessages,
  };
}
