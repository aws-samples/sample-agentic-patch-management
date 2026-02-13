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

/**
 * Hook for streaming chat with the patch automation agent via SSE.
 *
 * Session ID is generated per page load (not persisted across reloads).
 * This means conversation history resets on refresh. For production use,
 * consider persisting sessionId to localStorage if cross-reload continuity
 * is needed — the backend (AgentCore memory) already supports it.
 */
export function useAgentStream() {
  const [state, setState] = useState<StreamState>({
    messages: [],
    isStreaming: false,
    sessionId: `web-${crypto.randomUUID().replace(/-/g, "")}`,
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
        const headers: Record<string, string> = { "Content-Type": "application/json" };
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
    [state.sessionId]
  );

  const stopStreaming = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const clearMessages = useCallback(() => {
    setState((prev) => ({
      ...prev,
      messages: [],
      sessionId: `web-${crypto.randomUUID().replace(/-/g, "")}`,
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
