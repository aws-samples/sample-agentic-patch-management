import { useState, useRef, useEffect } from "react";
import Button from "@cloudscape-design/components/button";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import MessageBubble from "./MessageBubble";
import type { ChatMessage } from "../hooks/useAgentStream";
import type { Role } from "../hooks/useRole";

interface Props {
  messages: ChatMessage[];
  isStreaming: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
  onClear: () => void;
  sessionId: string;
  prefillText?: string;
  onPrefillConsumed?: () => void;
  role?: Role;
}

const EXAMPLE_PROMPTS = [
  { icon: "🔍", label: "Discover vulnerabilities", text: "What critical vulnerabilities do we have?", desc: "Scan fleet for active CVEs" },
  { icon: "🖥️", label: "Fleet status", text: "Show EC2 instances in dev environment", desc: "Check instance health and SSM status" },
  { icon: "📋", label: "Patch compliance", text: "Show me patch compliance status", desc: "Missing patches and compliance gaps" },
  { icon: "⚡", label: "Remediate a CVE", text: "Handle CVE-2025-38527 in dev", desc: "End-to-end patch workflow" },
];

export default function ChatPanel({
  messages,
  isStreaming,
  onSend,
  onStop,
  onClear,
  prefillText,
  onPrefillConsumed,
  role = "operator",
}: Props) {
  const [input, setInput] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const isViewer = role === "viewer";

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    if (prefillText) {
      setInput(prefillText);
      onPrefillConsumed?.();
    }
  }, [prefillText, onPrefillConsumed]);

  const handleSend = () => {
    const text = input.trim();
    if (!text || isStreaming) return;
    setInput("");
    onSend(text);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* Scrollable messages area — fills available space */}
      <div style={{ flex: 1, overflowY: "auto", padding: "12px 8px", minHeight: 0 }}>
        {messages.length === 0 ? (
          <EmptyState onSelect={(text) => setInput(text)} />
        ) : (
          messages.map((msg) => (
            <MessageBubble
              key={msg.id}
              message={msg}
              isStreaming={isStreaming && msg.id === messages[messages.length - 1]?.id}
              onAction={!isStreaming ? onSend : undefined}
            />
          ))
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input bar — pinned to bottom */}
      <div style={{ flexShrink: 0, padding: "4px 8px", borderTop: "1px solid #e9ebed", background: "#fff" }}>
        {isViewer ? (
          <Box textAlign="center" padding={{ vertical: "xs" }} color="text-body-secondary">
            🔒 Read-only mode — viewer role cannot send messages. Contact your admin for operator access.
          </Box>
        ) : (
          <>
            <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
              <div style={{ flex: 1 }}>
                <Input
                  value={input}
                  onChange={({ detail }) => setInput(detail.value)}
                  onKeyDown={({ detail }) => { if (detail.key === "Enter") handleSend(); }}
                  placeholder="Ask about vulnerabilities, patching, compliance..."
                  disabled={isStreaming}
                  autoFocus
                />
              </div>
              {isStreaming ? (
                <Button onClick={onStop} variant="normal">Stop</Button>
              ) : (
                <Button onClick={handleSend} variant="primary" disabled={!input.trim()}>Send</Button>
              )}
            </div>
            {messages.length > 0 && (
              <Box float="right" margin={{ top: "xxs" }}>
                <Button variant="link" onClick={onClear} disabled={isStreaming}>New conversation</Button>
              </Box>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function EmptyState({ onSelect }: { onSelect: (text: string) => void }) {
  return (
    <Box textAlign="center" padding={{ top: "l" }}>
      <SpaceBetween size="m">
        <div>
          <div style={{ fontSize: "32px", marginBottom: "8px" }}>🛡️</div>
          <Box variant="h3">What can I help you with?</Box>
          <Box variant="p" color="text-body-secondary" margin={{ top: "xxs" }}>
            Ask about vulnerabilities, patching, or compliance — or pick a starting point below.
          </Box>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px", maxWidth: "480px", margin: "0 auto" }}>
          {EXAMPLE_PROMPTS.map((prompt) => (
            <div
              key={prompt.text}
              onClick={() => onSelect(prompt.text)}
              style={{
                padding: "12px 14px",
                borderRadius: "10px",
                border: "1px solid #e9ebed",
                background: "#fff",
                cursor: "pointer",
                textAlign: "left",
                transition: "border-color 0.15s, box-shadow 0.15s",
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.borderColor = "#0972d3";
                e.currentTarget.style.boxShadow = "0 1px 4px rgba(9,114,211,0.12)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.borderColor = "#e9ebed";
                e.currentTarget.style.boxShadow = "none";
              }}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => { if (e.key === "Enter") onSelect(prompt.text); }}
              aria-label={prompt.label}
            >
              <div style={{ fontSize: "18px", marginBottom: "4px" }}>{prompt.icon}</div>
              <div style={{ fontSize: "13px", fontWeight: 600, color: "#000716" }}>{prompt.label}</div>
              <div style={{ fontSize: "12px", color: "#5f6b7a", marginTop: "2px" }}>{prompt.desc}</div>
            </div>
          ))}
        </div>
      </SpaceBetween>
    </Box>
  );
}
