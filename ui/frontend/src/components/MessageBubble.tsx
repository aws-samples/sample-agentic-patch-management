import Box from "@cloudscape-design/components/box";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Spinner from "@cloudscape-design/components/spinner";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
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
        marginBottom: "16px",
      }}
    >
      {/* Agent avatar */}
      {!isUser && (
        <div style={{
          width: "28px",
          height: "28px",
          borderRadius: "50%",
          background: "linear-gradient(135deg, #0972d3, #44b9d6)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: "14px",
          flexShrink: 0,
          marginRight: "8px",
          marginTop: "2px",
        }}>
          🤖
        </div>
      )}

      <div
        style={{
          maxWidth: isUser ? "80%" : "calc(100% - 40px)",
          padding: isUser ? "10px 14px" : "0",
          borderRadius: isUser ? "14px 14px 4px 14px" : "0",
          backgroundColor: isUser
            ? "var(--color-background-button-primary-default, #0972d3)"
            : "transparent",
          color: isUser ? "#fff" : "var(--color-text-body-default, #000716)",
          fontSize: "14px",
          lineHeight: "1.6",
        }}
      >
        {isUser ? (
          <Box color="inherit" variant="p">{message.content}</Box>
        ) : (
          <>
            {/* Tool workflow steps */}
            {message.tools.length > 0 && (
              <div style={{
                marginBottom: "10px",
                padding: "8px 12px",
                background: "var(--color-background-layout-toggle-default, #f2f3f3)",
                borderRadius: "8px",
                borderLeft: "3px solid #0972d3",
              }}>
                <div style={{ fontSize: "11px", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.5px", color: "#5f6b7a", marginBottom: "4px" }}>
                  Workflow
                </div>
                {message.tools.map((tool, i) => {
                  const isLast = i === message.tools.length - 1;
                  const isDone = !isLast || (isLast && message.content.length > 0);
                  return (
                    <div key={i} style={{ display: "flex", alignItems: "center", gap: "6px", padding: "2px 0" }}>
                      <StatusIndicator type={isDone ? "success" : "in-progress"}>
                        <span style={{ fontSize: "12px" }}>{tool}</span>
                      </StatusIndicator>
                    </div>
                  );
                })}
              </div>
            )}

            {/* Message content */}
            {message.content ? (
              <div className="agent-markdown">
                <Markdown remarkPlugins={[remarkGfm]}>
                  {cleanAgentText(message.content)}
                </Markdown>
              </div>
            ) : isStreaming ? (
              <div style={{ display: "flex", alignItems: "center", gap: "8px", padding: "8px 0" }}>
                <Spinner size="normal" />
                <span style={{ fontSize: "13px", color: "#5f6b7a" }}>Thinking...</span>
              </div>
            ) : null}

            {/* Error */}
            {message.error && (
              <div style={{ marginTop: "8px" }}>
                <StatusIndicator type="error">{message.error}</StatusIndicator>
              </div>
            )}

            {/* Duration */}
            {message.durationMs != null && (
              <div style={{ fontSize: "11px", color: "#8d9baa", marginTop: "8px" }}>
                Completed in {(message.durationMs / 1000).toFixed(1)}s
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
  // Strip thinking content entirely (before extracting answer)
  text = text.replace(/<thinking>[\s\S]*?<\/thinking>/g, "");
  // Extract answer block if present
  const answerMatch = text.match(/<answer>([\s\S]*?)<\/answer>/);
  if (answerMatch) {
    text = answerMatch[1];
  }
  // Strip any remaining tags
  for (const tag of ["<answer>", "</answer>", "<thinking>", "</thinking>"]) {
    text = text.replaceAll(tag, "");
  }
  return text.trim();
}
