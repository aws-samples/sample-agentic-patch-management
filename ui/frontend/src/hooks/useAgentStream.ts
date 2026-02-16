import { useState, useCallback, useRef } from "react";

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
  consult_vulnerability_analyst: "🔍 Consulting Vulnerability Analyst",
  consult_patch_manager: "🛠️ Consulting Patch Manager",
  consult_compliance_analyst: "📊 Consulting Compliance Analyst",
  use_aws: "⚙️ Consulting AWS Services",
};

// ── Session persistence ────────────────────────────────────────────
// Persist sessionId to localStorage so conversation history survives
// page refreshes. AgentCore memory already stores the conversation
// server-side — this just ensures the same session_id is sent.

const SESSION_STORAGE_KEY = "patchy-session-id";

function getOrCreateSessionId(): string {
  try {
    const stored = localStorage.getItem(SESSION_STORAGE_KEY);
    if (stored) return stored;
  } catch {
    // localStorage unavailable (private browsing, etc.)
  }
  const id = `web-${crypto.randomUUID().replace(/-/g, "")}`;
  try {
    localStorage.setItem(SESSION_STORAGE_KEY, id);
  } catch {
    // best-effort
  }
  return id;
}

function resetSessionId(): string {
  const id = `web-${crypto.randomUUID().replace(/-/g, "")}`;
  try {
    localStorage.setItem(SESSION_STORAGE_KEY, id);
  } catch {
    // best-effort
  }
  return id;
}

/**
 * Hook for streaming chat with the patch automation agent via SSE.
 *
 * Session ID is persisted to localStorage so conversation context
 * survives page refreshes. AgentCore memory stores the full conversation
 * server-side — the sessionId is the key that reconnects to it.
 * Clicking "New conversation" generates a fresh session.
 */
export function useAgentStream(role: string = "operator") {
  const [state, setState] = useState<StreamState>({
    messages: [],
    isStreaming: false,
    sessionId: getOrCreateSessionId(),
  });

  const abortRef = useRef<AbortController | null>(null);

  const sendMessage = useCallback(
    async (text: string) => {
      const userMsg: ChatMessage = {
        id: crypto.randomUUID(),
        role: "user",
        content: text,
        tools: [],
        timestamp: Date.now(),
      };

      const assistantMsg: ChatMessage = {
        id: crypto.randomUUID(),
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

      const assistantId = assistantMsg.id;
      abortRef.current = new AbortController();

      try {
        const apiKey = import.meta.env.VITE_API_KEY;
        const headers: Record<string, string> = { "Content-Type": "application/json", "X-Role": role };
        if (apiKey) headers["X-API-Key"] = apiKey;

        const res = await fetch("/api/chat", {
          method: "POST",
          headers,
          body: JSON.stringify({
            message: text,
            session_id: state.sessionId,
          }),
          signal: abortRef.current.signal,
        });

        if (!res.ok || !res.body) {
          throw new Error(`API error: ${res.status}`);
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let partial = "";

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
                setState((prev) => ({
                  ...prev,
                  messages: prev.messages.map((m) =>
                    m.id === assistantId
                      ? { ...m, content: m.content + event.content }
                      : m
                  ),
                }));
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
                setState((prev) => ({
                  ...prev,
                  messages: prev.messages.map((m) =>
                    m.id === assistantId
                      ? { ...m, durationMs: event.duration_ms }
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
