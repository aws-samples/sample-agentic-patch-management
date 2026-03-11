import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useState, type ReactNode } from "react";
import { ExclamationTriangleIcon } from "@heroicons/react/24/solid";
import { Tooltip } from "./ui/Tooltip";
import type { ChatMessage } from "../hooks/useAgentStream";

interface Props {
  message: ChatMessage;
  isStreaming: boolean;
  onAction?: (text: string) => void;
}

export default function MessageBubble({ message, isStreaming, onAction }: Props) {
  const isUser = message.role === "user";
  const cleaned = !isUser ? cleanAgentText(message.content) : "";
  const { body, actions } = !isUser && cleaned ? extractActions(cleaned) : { body: cleaned, actions: [] };
  const showActions = !isStreaming && actions.length > 0;

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"} mb-4 animate-fade-in`}>
      {/* Agent avatar */}
      {!isUser && (
        <div className="w-7 h-7 rounded-full bg-gradient-to-br from-blue-500 to-cyan-400 flex items-center justify-center text-sm shrink-0 mr-2 mt-0.5 shadow-sm">
          🤖
        </div>
      )}

      <div className={`${isUser ? "max-w-[80%]" : "max-w-[calc(100%-40px)]"}`}>
        {isUser ? (
          <div className="px-3.5 py-2.5 rounded-2xl rounded-br-sm bg-blue-600 text-white text-sm leading-relaxed">
            {message.content}
          </div>
        ) : (
          <>
            {/* Workflow stepper */}
            {message.tools.length > 0 && (
              <div className="mb-2.5 p-3 bg-white rounded-xl border border-gray-200 shadow-sm">
                <div className="text-[11px] font-semibold uppercase tracking-wider text-gray-500 mb-1.5 flex items-center gap-1.5">
                  <span className="w-1 h-3.5 rounded-full bg-blue-500" />
                  Workflow
                </div>
                {message.tools.map((tool, i) => {
                  const isLast = i === message.tools.length - 1;
                  const isDone = !isLast || (isLast && message.content.length > 0);
                  return (
                    <div key={i} className="flex gap-2 relative">
                      {i < message.tools.length - 1 && (
                        <div className={`absolute left-[7px] top-[18px] bottom-[-2px] w-0.5 ${isDone ? "bg-green-500" : "bg-gray-200"}`} />
                      )}
                      <div className={`w-4 h-4 rounded-full flex items-center justify-center shrink-0 mt-0.5 text-[9px] text-white ${isDone ? "bg-green-500" : "bg-blue-500 animate-pulse"}`}>
                        {isDone ? "✓" : i + 1}
                      </div>
                      <div className={`text-xs py-0.5 pb-2 ${isDone ? "text-gray-700" : "text-blue-600 font-medium"}`}>
                        {tool}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

            {/* Message content */}
            {body ? (
              <>
                <div className="agent-markdown">
                  <Markdown remarkPlugins={[remarkGfm]} components={{ table: CollapsibleTable }}>
                    {body}
                  </Markdown>
                </div>
                {isStreaming && <Spinner />}
              </>
            ) : isStreaming ? (
              <div className="flex items-center gap-2 py-2">
                <Spinner />
                <span className="text-[13px] text-gray-500">Thinking...</span>
              </div>
            ) : null}

            {/* Action buttons */}
            {showActions && onAction && (
              <div className="mt-3 p-3 bg-gray-50 rounded-lg border border-gray-100">
                <div className="text-[11px] font-semibold uppercase tracking-wider text-gray-500 mb-2">
                  Suggested Actions
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {actions.map((action, i) => {
                    const short = shortenAction(action);
                    const needsTooltip = short !== action;
                    const btn = (
                      <button
                        key={i}
                        onClick={() => onAction(action)}
                        className="px-3 py-1.5 text-xs font-medium text-gray-700 bg-white border border-gray-200 rounded-lg hover:border-blue-400 hover:text-blue-700 hover:shadow-sm transition-all group"
                      >
                        <span className="group-hover:translate-x-0.5 inline-block transition-transform">
                          {short}
                        </span>
                      </button>
                    );
                    return needsTooltip ? <Tooltip key={i} content={action} side="bottom">{btn}</Tooltip> : btn;
                  })}
                </div>
              </div>
            )}

            {/* Error */}
            {message.error && (
              <div className="mt-2 flex items-center gap-1.5 text-red-600 text-sm">
                <ExclamationTriangleIcon className="w-4 h-4" />
                {message.error}
              </div>
            )}

            {/* Duration */}
            {message.durationMs != null && (
              <div className="text-[11px] text-gray-400 mt-2">
                Completed in {(message.durationMs / 1000).toFixed(1)}s
              </div>
            )}
          </>
        )}
      </div>

      {/* Timestamp */}
      {!isStreaming && (
        <div className={`text-[10px] text-gray-400 self-end shrink-0 whitespace-nowrap ${isUser ? "mr-1" : "ml-1"}`}>
          {new Date(message.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
        </div>
      )}
    </div>
  );
}

/**
 * Shorten verbose action labels for button display.
 * Strips trailing stats like "- 5 instances, 735 HIGH CVEs",
 * parenthetical details, and caps at ~60 chars.
 * Full text is preserved in the title attribute for hover.
 */
function shortenAction(text: string): string {
  let s = text;
  // Strip "- N instances, N SEVERITY CVEs" patterns
  s = s.replace(/\s*[-–—]\s*\d+\s+instances?.*$/i, "");
  // Strip trailing parenthetical like "(5 instances across dev)"
  s = s.replace(/\s*\([^)]*\d+[^)]*\)\s*$/, "");
  // Strip "for all X environments" / "across all environments"
  s = s.replace(/\s+(for|across)\s+all\s+\d*\s*environments?/i, "");
  // Cap length
  if (s.length > 60) s = s.slice(0, 57) + "…";
  return s.trim();
}

function Spinner() {
  return (
    <div className="flex items-center gap-1.5 py-1">
      <div className="flex gap-1">
        <div className="w-1.5 h-1.5 bg-blue-500 rounded-full animate-bounce [animation-delay:0ms]" />
        <div className="w-1.5 h-1.5 bg-blue-500 rounded-full animate-bounce [animation-delay:150ms]" />
        <div className="w-1.5 h-1.5 bg-blue-500 rounded-full animate-bounce [animation-delay:300ms]" />
      </div>
    </div>
  );
}

const VISIBLE_ROWS = 10;

function CollapsibleTable({ children }: { children?: ReactNode }) {
  const [expanded, setExpanded] = useState(false);
  const totalRows = countBodyRows(children);
  if (totalRows <= VISIBLE_ROWS) return <table>{children}</table>;

  return (
    <div>
      <table style={expanded ? undefined : { display: "block", overflow: "hidden" }}>
        {expanded ? children : truncateTableBody(children, VISIBLE_ROWS)}
      </table>
      <button
        onClick={() => setExpanded((v) => !v)}
        className="text-xs font-semibold text-blue-600 hover:text-blue-800 py-1.5 bg-transparent border-0 cursor-pointer"
      >
        {expanded ? "Show less" : `Show all ${totalRows} rows`}
      </button>
    </div>
  );
}

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

function cleanAgentText(raw: string): string {
  let text = raw;
  text = text.replace(/<thinking>[\s\S]*?<\/thinking>/g, "");
  const answerMatch = text.match(/<answer>([\s\S]*?)<\/answer>/);
  if (answerMatch) text = answerMatch[1];
  for (const tag of ["<answer>", "</answer>", "<thinking>", "</thinking>"]) {
    text = text.replaceAll(tag, "");
  }
  return text.trim();
}

function extractActions(text: string): { body: string; actions: string[] } {
  const pattern = /(?:^|\n)(?:#{1,4}\s*(?:[\p{Emoji_Presentation}\p{Emoji}\u200d]+\s*)?(?:Next\s*Steps|Recommended\s*Actions)[:\s]*|[*_]{2}(?:[\p{Emoji_Presentation}\p{Emoji}\u200d]+\s*)?(?:Next\s*Steps|Recommended\s*Actions)[:\s]*[*_]{2}[:\s]*)\n([\s\S]*?)$/iu;
  const match = text.match(pattern);
  if (!match) return { body: text, actions: [] };

  const body = text.slice(0, match.index!).trim();
  const stepsBlock = match[1].trim();
  const actions: string[] = [];
  for (const line of stepsBlock.split("\n")) {
    const cleaned = line.replace(/^[\s]*[-*•]\s*/, "").replace(/^[\s]*\d+[.)]\s*/, "").replace(/[*_`]/g, "").replace(/^\s*>\s*/, "").trim();
    if (cleaned.length > 5 && cleaned.length < 120) actions.push(cleaned);
  }
  return { body, actions: actions.slice(0, 5) };
}
