import { useState, useRef, useEffect } from "react";
import Button from "@cloudscape-design/components/button";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import Header from "@cloudscape-design/components/header";
import MessageBubble from "./MessageBubble";
import type { ChatMessage } from "../hooks/useAgentStream";

interface Props {
  messages: ChatMessage[];
  isStreaming: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
  onClear: () => void;
  sessionId: string;
}

const EXAMPLE_PROMPTS = [
  "What critical vulnerabilities do we have?",
  "Show EC2 instances in dev environment",
  "Show me patch compliance status",
];

export default function ChatPanel({
  messages,
  isStreaming,
  onSend,
  onStop,
  onClear,
  sessionId,
}: Props) {
  const [input, setInput] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleSend = () => {
    const text = input.trim();
    if (!text || isStreaming) return;
    setInput("");
    onSend(text);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* Header */}
      <Header
        variant="h3"
        actions={
          <Button
            iconName="remove"
            variant="icon"
            onClick={onClear}
            disabled={isStreaming}
            ariaLabel="Clear chat"
          />
        }
        description={`Session: ${sessionId.slice(0, 16)}...`}
      >
        🤖 Patchy
      </Header>

      {/* Messages area */}
      <div
        style={{
          flex: 1,
          overflowY: "auto",
          padding: "16px 8px",
          minHeight: 0,
        }}
      >
        {messages.length === 0 ? (
          <EmptyState onSelect={(text) => { setInput(text); }} />
        ) : (
          messages.map((msg) => (
            <MessageBubble
              key={msg.id}
              message={msg}
              isStreaming={isStreaming && msg.id === messages[messages.length - 1]?.id}
            />
          ))
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input area */}
      <div style={{ padding: "8px 0", borderTop: "1px solid #e9ebed" }}>
        <SpaceBetween direction="horizontal" size="xs">
          <div style={{ flex: 1 }}>
            <Input
              value={input}
              onChange={({ detail }) => setInput(detail.value)}
              onKeyDown={({ detail }) => {
                if (detail.key === "Enter") handleSend();
              }}
              placeholder="Ask Patchy about vulnerabilities, patching, compliance..."
              disabled={isStreaming}
              autoFocus
            />
          </div>
          {isStreaming ? (
            <Button onClick={onStop} variant="normal">
              Stop
            </Button>
          ) : (
            <Button onClick={handleSend} variant="primary" disabled={!input.trim()}>
              Send
            </Button>
          )}
        </SpaceBetween>
      </div>
    </div>
  );
}

function EmptyState({ onSelect }: { onSelect: (text: string) => void }) {
  return (
    <Box textAlign="center" padding={{ top: "xxl" }}>
      <SpaceBetween size="m">
        <Box variant="h2">🛡️</Box>
        <Box variant="p" color="text-body-secondary">
          Ask Patchy about vulnerabilities, patching operations, or compliance.
        </Box>
        <SpaceBetween size="xs">
          {EXAMPLE_PROMPTS.map((prompt) => (
            <Button
              key={prompt}
              variant="link"
              onClick={() => onSelect(prompt)}
            >
              {prompt}
            </Button>
          ))}
        </SpaceBetween>
      </SpaceBetween>
    </Box>
  );
}
