import { useState, useCallback, useRef, useEffect } from "react";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  tools: string[];
  durationMs?: number;
  error?: string;
  timestamp: number;
}

interface StreamState {
  messages: ChatMessage[];
  isStreaming: boolean;
  sessionId: string;
}

const TOOL_LABELS: Record<string, string> = {
  use_aws: "⚙️ Consulting AWS Services",
};

// ── Session persistence ────────────────────────────────────────────
// Persist sessionId AND the user's email to localStorage so we can:
//   1. Rehydrate the chat panel on refresh from AgentCore Memory.
//   2. Detect "different user signs in" and start fresh.
//
// AgentCore Memory is the source of truth for messages. localStorage
// only holds the keys (session_id, email) — never the messages.

const SESSION_STORAGE_KEY = "patchy-session-id";
const SESSION_USER_KEY = "patchy-session-user";

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
    const stored = localStorage.getItem(SESSION_STORAGE_KEY);
    if (stored) return stored;
  } catch {
    // localStorage unavailable (private browsing, etc.)
  }
  const id = newSessionId();
  try {
    localStorage.setItem(SESSION_STORAGE_KEY, id);
  } catch {
    // best-effort
  }
  return id;
}

function resetSessionId(): string {
  const id = newSessionId();
  try {
    localStorage.setItem(SESSION_STORAGE_KEY, id);
    localStorage.removeItem(SESSION_USER_KEY);
  } catch {
    // best-effort
  }
  return id;
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
export function useAgentStream(role: string = "operator") {
  const [state, setState] = useState<StreamState>({
    messages: [],
    isStreaming: false,
    sessionId: getOrCreateSessionId(),
  });

  const abortRef = useRef<AbortController | null>(null);

  // ── Rehydrate from AgentCore Memory on mount ──────────────────────
  // 1. Get current Cognito email from /api/auth/config.
  // 2. If stored email differs from current → mint fresh session,
  //    don't try to rehydrate (different user).
  // 3. Else fetch /api/session/<id>/messages and replace state.messages.
  // 4. On 404 / empty → mint fresh session (memory wiped or new user).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      // Best-effort fetch of current user identity.
      let currentEmail: string | null = null;
      try {
        const authRes = await fetch("/api/auth/config");
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
        if (cancelled) return;
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
        const res = await fetch(`/api/session/${sid}/messages`);
        if (res.status === 200) {
          const data = await res.json();
          if (!cancelled && Array.isArray(data?.messages) && data.messages.length > 0) {
            setState((prev) => ({
              ...prev,
              messages: data.messages.map((m: { role: string; content: string }) => ({
                id: uuid(),
                role: m.role === "assistant" ? "assistant" : "user",
                content: m.content,
                tools: [],
                timestamp: Date.now(),
              })),
            }));
          }
        } else if (res.status === 404) {
          // Memory wiped (agent redeployed) or session never existed.
          // Mint fresh and clear so the operator gets a clean slate.
          if (!cancelled) {
            setState((prev) => ({
              ...prev,
              sessionId: resetSessionId(),
              messages: [],
            }));
          }
        }
        // On 503 (memory not configured) or other errors: leave state
        // alone — empty chat with the fresh session ID.
      } catch (err) {
        console.warn("[rehydration] Failed to load session history:", err);
      }
    })();
    return () => {
      cancelled = true;
    };
    // Run once on mount only — re-running on sessionId change would
    // overwrite the freshly minted session before the user types.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const sendMessage = useCallback(
    async (text: string) => {
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
      }));

      // Stamp the current user on first send so the next page load can
      // detect a user switch (different Cognito email → fresh session).
      // Best-effort — failure here doesn't block the message.
      try {
        const authRes = await fetch("/api/auth/config");
        if (authRes.ok) {
          const data = await authRes.json();
          if (data?.email) setStoredUser(data.email);
        }
      } catch {
        // ignore
      }

      const assistantId = assistantMsg.id;
      abortRef.current = new AbortController();

      try {
        const apiKey = import.meta.env.VITE_API_KEY;
        const headers: Record<string, string> = { "Content-Type": "application/json", "X-Role": role };
        if (apiKey) headers["X-API-Key"] = apiKey;

        const fetchWithRetry = async (retries = 2): Promise<Response> => {
          const res = await fetch("/api/chat", {
            method: "POST",
            headers,
            body: JSON.stringify({
              message: text,
              session_id: state.sessionId,
              timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            }),
            signal: abortRef.current?.signal,
          });
          if (res.status >= 500 && retries > 0) {
            await new Promise((r) => setTimeout(r, 1000));
            return fetchWithRetry(retries - 1);
          }
          return res;
        };

        const res = await fetchWithRetry();

        if (!res.ok || !res.body) {
          const detail = res.status === 503 ? "Agent is starting up. Try again in a few seconds."
            : res.status >= 500 ? "Server error. The agent may be restarting."
            : `Request failed (${res.status}).`;
          throw new Error(detail);
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let partial = "";
        let contentBuffer = "";  // Paragraph-level buffer
        let renderedContent = ""; // What's already displayed

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

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          partial += decoder.decode(value, { stream: true });
          const lines = partial.split("\n");
          partial = lines.pop() ?? "";

          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed.startsWith("data: ")) continue;

            try {
              const event = JSON.parse(trimmed.slice(6));

              if (event.type === "text") {
                contentBuffer += event.content;
                flushBlocks(); // Render any complete blocks
              } else if (event.type === "tool_start") {
                const label =
                  TOOL_LABELS[event.tool] ?? `🔧 ${event.tool}`;
                setState((prev) => ({
                  ...prev,
                  messages: prev.messages.map((m) =>
                    m.id === assistantId && !m.tools.includes(label)
                      ? { ...m, tools: [...m.tools, label] }
                      : m
                  ),
                }));
              } else if (event.type === "done") {
                // Flush remaining buffer on completion
                renderedContent += contentBuffer;
                contentBuffer = "";
                setState((prev) => ({
                  ...prev,
                  messages: prev.messages.map((m) =>
                    m.id === assistantId
                      ? { ...m, content: renderedContent, durationMs: event.duration_ms }
                      : m
                  ),
                }));
              } else if (event.type === "error") {
                setState((prev) => ({
                  ...prev,
                  messages: prev.messages.map((m) =>
                    m.id === assistantId
                      ? { ...m, error: event.message }
                      : m
                  ),
                }));
              }
            } catch {
              // skip malformed SSE lines
            }
          }
        }
      } catch (err: unknown) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        const message = err instanceof Error ? err.message : "Unknown error";
        setState((prev) => ({
          ...prev,
          messages: prev.messages.map((m) =>
            m.id === assistantId ? { ...m, error: message } : m
          ),
        }));
      } finally {
        setState((prev) => ({ ...prev, isStreaming: false }));
        abortRef.current = null;
      }
    },
    [state.sessionId, role]
  );

  const stopStreaming = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const clearMessages = useCallback(() => {
    setState((prev) => ({
      ...prev,
      messages: [],
      sessionId: resetSessionId(),
    }));
  }, []);

  return {
    messages: state.messages,
    isStreaming: state.isStreaming,
    sessionId: state.sessionId,
    sendMessage,
    stopStreaming,
    clearMessages,
  };
}
