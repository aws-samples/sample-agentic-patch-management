import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Spinner from "@cloudscape-design/components/spinner";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useState, type ReactNode } from "react";
import type { ChatMessage } from "../hooks/useAgentStream";

interface Props {
  message: ChatMessage;
  isStreaming: boolean;
  onAction?: (text: string) => void;
}

export default function MessageBubble({ message, isStreaming, onAction }: Props) {
  const isUser = message.role === "user";

  // Parse next steps from completed assistant messages
  const cleaned = !isUser ? cleanAgentText(message.content) : "";
  const { body, actions } = !isUser && !isStreaming && cleaned
    ? extractActions(cleaned)
    : { body: cleaned, actions: [] };

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

            {/* Message content (without next steps section) */}
            {(isStreaming ? cleaned : body) ? (
              <div className="agent-markdown">
                <Markdown
                  remarkPlugins={[remarkGfm]}
                  components={{ table: CollapsibleTable }}
                >
                  {isStreaming ? cleaned : body}
                </Markdown>
              </div>
            ) : isStreaming ? (
              <div style={{ display: "flex", alignItems: "center", gap: "8px", padding: "8px 0" }}>
                <Spinner size="normal" />
                <span style={{ fontSize: "13px", color: "#5f6b7a" }}>Thinking...</span>
              </div>
            ) : null}

            {/* Action buttons from Next Steps */}
            {actions.length > 0 && onAction && (
              <div style={{
                marginTop: "12px",
                padding: "10px 12px",
                background: "var(--color-background-layout-toggle-default, #f2f3f3)",
                borderRadius: "8px",
              }}>
                <div style={{ fontSize: "11px", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.5px", color: "#5f6b7a", marginBottom: "8px" }}>
                  Suggested Actions
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: "6px" }}>
                  {actions.map((action, i) => (
                    <Button
                      key={i}
                      variant="normal"
                      onClick={() => onAction(action)}
                    >
                      {action}
                    </Button>
                  ))}
                </div>
              </div>
            )}

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

const VISIBLE_ROWS = 10;

/** Show first 10 rows of large tables with a "Show more" toggle. */
function CollapsibleTable({ children }: { children?: ReactNode }) {
  const [expanded, setExpanded] = useState(false);

  // Count body rows to decide if truncation is needed
  const totalRows = countBodyRows(children);
  if (totalRows <= VISIBLE_ROWS) {
    return <table>{children}</table>;
  }

  return (
    <div>
      <table style={expanded ? undefined : { display: "block", overflow: "hidden" }}>
        {expanded ? children : truncateTableBody(children, VISIBLE_ROWS)}
      </table>
      <button
        onClick={() => setExpanded((v) => !v)}
        style={{
          background: "none",
          border: "none",
          color: "#0972d3",
          cursor: "pointer",
          fontSize: "12px",
          fontWeight: 600,
          padding: "6px 0",
        }}
      >
        {expanded ? "Show less" : `Show all ${totalRows} rows`}
      </button>
    </div>
  );
}

/** Count <tr> elements inside <tbody>. */
function countBodyRows(children: ReactNode): number {
  const arr = Array.isArray(children) ? children : [children];
  for (const section of arr) {
    if (section && typeof section === "object" && "props" in section && section.type === "tbody") {
      const rows = section.props.children;
      return Array.isArray(rows) ? rows.length : rows ? 1 : 0;
    }
  }
  return 0;
}

/** Clone the table children but limit tbody to `max` rows. */
function truncateTableBody(children: ReactNode, max: number): ReactNode {
  const arr = Array.isArray(children) ? children : [children];
  return arr.map((section, i) => {
    if (section && typeof section === "object" && "props" in section && section.type === "tbody") {
      const rows = Array.isArray(section.props.children)
        ? section.props.children.slice(0, max)
        : section.props.children;
      return <tbody key={i}>{rows}</tbody>;
    }
    return section;
  });
}

/** Strip <answer>/<thinking> XML tags the agent wraps responses in. */
function cleanAgentText(raw: string): string {
  let text = raw;
  text = text.replace(/<thinking>[\s\S]*?<\/thinking>/g, "");
  const answerMatch = text.match(/<answer>([\s\S]*?)<\/answer>/);
  if (answerMatch) {
    text = answerMatch[1];
  }
  for (const tag of ["<answer>", "</answer>", "<thinking>", "</thinking>"]) {
    text = text.replaceAll(tag, "");
  }
  return text.trim();
}

/**
 * Extract "Next Steps" section from agent markdown and return
 * the body (without next steps) and an array of action strings.
 */
function extractActions(text: string): { body: string; actions: string[] } {
  const pattern = /(?:^|\n)(?:#{1,4}\s*(?:[\p{Emoji_Presentation}\p{Emoji}\u200d]+\s*)?Next\s*Steps[:\s]*|[*_]{2}(?:[\p{Emoji_Presentation}\p{Emoji}\u200d]+\s*)?Next\s*Steps[:\s]*[*_]{2}[:\s]*)\n([\s\S]*?)$/iu;
  const match = text.match(pattern);

  if (!match) return { body: text, actions: [] };

  const body = text.slice(0, match.index!).trim();
  const stepsBlock = match[1].trim();

  const actions: string[] = [];
  for (const line of stepsBlock.split("\n")) {
    const cleaned = line
      .replace(/^[\s]*[-*•]\s*/, "")
      .replace(/^[\s]*\d+[.)]\s*/, "")
      .replace(/[*_`]/g, "")
      .replace(/^\s*>\s*/, "")
      .trim();

    if (cleaned.length > 5 && cleaned.length < 120) {
      actions.push(cleaned);
    }
  }

  return { body, actions: actions.slice(0, 5) };
}
