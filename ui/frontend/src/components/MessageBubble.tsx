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
          maxWidth: "85%",
          padding: "12px 16px",
          borderRadius: isUser ? "16px 16px 4px 16px" : "16px 16px 16px 4px",
          backgroundColor: isUser ? "#0972d3" : "#f2f3f3",
          color: isUser ? "#fff" : "#000716",
        }}
      >
        {isUser ? (
          <Box color="inherit" variant="p">
            {message.content}
          </Box>
        ) : (
          <>
            {/* Tool indicators */}
            {message.tools.length > 0 && (
              <div style={{ marginBottom: "8px" }}>
                {message.tools.map((tool, i) => (
                  <div key={i} style={{ fontSize: "13px", opacity: 0.8, marginBottom: "2px" }}>
                    {tool}
                  </div>
                ))}
              </div>
            )}

            {/* Message content */}
            {message.content ? (
              <div className="agent-markdown">
                <Markdown>{cleanAgentText(message.content)}</Markdown>
              </div>
            ) : isStreaming ? (
              <StatusIndicator type="loading">Thinking...</StatusIndicator>
            ) : null}

            {/* Error */}
            {message.error && (
              <StatusIndicator type="error">{message.error}</StatusIndicator>
            )}

            {/* Duration */}
            {message.durationMs != null && (
              <div style={{ fontSize: "12px", opacity: 0.5, marginTop: "6px" }}>
                ⏱️ {(message.durationMs / 1000).toFixed(1)}s
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/** Strip <answer>/<thinking> XML tags the agent wraps responses in. */
function cleanAgentText(raw: string): string {
  let text = raw;
  const answerMatch = text.match(/<answer>([\s\S]*?)<\/answer>/);
  if (answerMatch) {
    text = answerMatch[1];
  }
  for (const tag of ["<answer>", "</answer>", "<thinking>", "</thinking>"]) {
    text = text.replaceAll(tag, "");
  }
  return text.trim();
}
