import Box from "@cloudscape-design/components/box";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Markdown from "react-markdown";
import type { ChatMessage } from "../hooks/useAgentStream";

interface Props {
  message: ChatMessage;
  isStreaming: boolean;
}

export default function MessageBubble({ message, isStreaming }: Props) {
  const isUser = message.role === "user";

  return (
    <div
      style={{
        display: "flex",
        justifyContent: isUser ? "flex-end" : "flex-start",
        marginBottom: "12px",
      }}
    >
      <div
        style={{
          maxWidth: "90%",
          padding: "10px 14px",
          borderRadius: isUser ? "14px 14px 4px 14px" : "14px 14px 14px 4px",
          backgroundColor: isUser ? "var(--color-background-button-primary-default, #0972d3)" : "var(--color-background-layout-toggle-default, #f2f3f3)",
          color: isUser ? "#fff" : "var(--color-text-body-default, #000716)",
          fontSize: "14px",
          lineHeight: "1.5",
        }}
      >
        {isUser ? (
          <Box color="inherit" variant="p">{message.content}</Box>
        ) : (
          <>
            {/* Tool indicators as a workflow timeline */}
            {message.tools.length > 0 && (
              <div style={{
                marginBottom: "8px",
                paddingBottom: "8px",
                borderBottom: "1px solid rgba(0,0,0,0.08)",
              }}>
                {message.tools.map((tool, i) => {
                  const isLast = i === message.tools.length - 1;
                  const isDone = !isLast || (isLast && message.content.length > 0);
                  return (
                    <div key={i} style={{ marginBottom: "2px" }}>
                      <StatusIndicator
                        type={isDone ? "success" : "in-progress"}
                      >
                        <span style={{ fontSize: "12px" }}>{tool}</span>
                      </StatusIndicator>
                    </div>
                  );
                })}
              </div>
            )}

            {/* Message content */}
            {message.content ? (
              <div className="agent-markdown" style={markdownStyles}>
                <Markdown>{cleanAgentText(message.content)}</Markdown>
              </div>
            ) : isStreaming ? (
              <StatusIndicator type="loading">Thinking...</StatusIndicator>
            ) : null}

            {/* Error */}
            {message.error && (
              <div style={{ marginTop: "6px" }}>
                <StatusIndicator type="error">{message.error}</StatusIndicator>
              </div>
            )}

            {/* Duration */}
            {message.durationMs != null && (
              <div style={{ fontSize: "11px", opacity: 0.45, marginTop: "6px" }}>
                ⏱️ {(message.durationMs / 1000).toFixed(1)}s
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/** Inline styles for markdown content to match Cloudscape typography. */
const markdownStyles: React.CSSProperties = {
  fontSize: "14px",
  lineHeight: "1.6",
  wordBreak: "break-word",
};

/** Strip <answer>/<thinking> XML tags the agent wraps responses in. */
function cleanAgentText(raw: string): string {
  let text = raw;
  // Extract answer block if present
  const answerMatch = text.match(/<answer>([\s\S]*?)<\/answer>/);
  if (answerMatch) {
    text = answerMatch[1];
  }
  // Strip any remaining tags
  for (const tag of ["<answer>", "</answer>", "<thinking>", "</thinking>"]) {
    text = text.replaceAll(tag, "");
  }
  // Strip thinking content entirely
  text = text.replace(/<thinking>[\s\S]*?<\/thinking>/g, "");
  return text.trim();
}
