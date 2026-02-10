import { useState, useRef, useEffect, useCallback } from "react";
import Button from "@cloudscape-design/components/button";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import MessageBubble from "./MessageBubble";
import type { ChatMessage } from "../hooks/useAgentStream";

interface Props {
  messages: ChatMessage[];
  isStreaming: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
  onClear: () => void;
  sessionId: string;
  prefillText?: string;
  onPrefillConsumed?: () => void;
}

const EXAMPLE_PROMPTS = [
  "What critical vulnerabilities do we have?",
  "Show EC2 instances in dev environment",
  "Show me patch compliance status",
  "Handle CVE-2025-38527 in dev",
];

export default function ChatPanel({
  messages,
  isStreaming,
  onSend,
  onStop,
  onClear,
  prefillText,
  onPrefillConsumed,
}: Props) {
  const [input, setInput] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const inputBarRef = useRef<HTMLDivElement>(null);
  const [messagesHeight, setMessagesHeight] = useState<number | undefined>(undefined);

  // Compute messages area height from viewport minus known chrome
  // TopNav ~48px, SplitPanel header ~56px, input bar measured dynamically
  const recalcHeight = useCallback(() => {
    const inputH = inputBarRef.current?.offsetHeight ?? 60;
    const chrome = 48 + 120; // TopNavigation + SplitPanel header + padding/margins
    setMessagesHeight(window.innerHeight - chrome - inputH);
  }, []);

  useEffect(() => {
    recalcHeight();
    window.addEventListener("resize", recalcHeight);
    const t = setTimeout(recalcHeight, 500);
    return () => { window.removeEventListener("resize", recalcHeight); clearTimeout(t); };
  }, [recalcHeight]);

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
    <div ref={rootRef}>
      {/* Scrollable messages area */}
      <div
        style={{
          overflowY: "auto",
          padding: "12px 8px",
          height: messagesHeight ?? "calc(100vh - 280px)",
        }}
      >
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

      {/* Input bar */}
      <div ref={inputBarRef} style={{ padding: "4px 8px", borderTop: "1px solid #e9ebed", background: "#fff" }}>
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
      </div>
    </div>
  );
}

function EmptyState({ onSelect }: { onSelect: (text: string) => void }) {
  return (
    <Box textAlign="center" padding={{ top: "l" }}>
      <SpaceBetween size="s">
        <Box variant="p" color="text-body-secondary">
          Ask Patchy about vulnerabilities, patching, or compliance.
          Or click items on the dashboard to get started.
        </Box>
        <SpaceBetween size="xxs">
          {EXAMPLE_PROMPTS.map((prompt) => (
            <Button key={prompt} variant="link" onClick={() => onSelect(prompt)}>{prompt}</Button>
          ))}
        </SpaceBetween>
      </SpaceBetween>
    </Box>
  );
}
