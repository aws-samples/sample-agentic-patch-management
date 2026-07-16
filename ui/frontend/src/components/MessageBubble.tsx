import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Highlight, themes } from "prism-react-renderer";
import { useState, useEffect, type ReactNode } from "react";
import { ExclamationTriangleIcon } from "@heroicons/react/24/solid";
import { Tooltip } from "./ui/Tooltip";
import { CopyButton } from "./ui/CopyButton";
import type { ChatMessage } from "../hooks/useAgentStream";

function extractText(node: ReactNode): string {
  if (typeof node === "string") return node;
  if (typeof node === "number") return String(node);
  if (!node) return "";
  if (Array.isArray(node)) return node.map(extractText).join("");
  if (typeof node === "object" && "props" in node) return extractText(node.props.children);
  return "";
}

function CodeBlock(props: React.HTMLAttributes<HTMLPreElement> & { children?: ReactNode }) {
  const text = extractText(props.children).replace(/\n$/, "");
  // Extract language from className (react-markdown sets "language-xxx")
  const childProps = (props.children as React.ReactElement)?.props;
  const langMatch = childProps?.className?.match(/language-(\w+)/);
  const language = langMatch?.[1] ?? "bash";

  return (
    <div className="relative group">
      <Highlight theme={themes.nightOwl} code={text} language={language}>
        {({ style, tokens, getLineProps, getTokenProps }) => (
          <pre style={{ ...style, background: "var(--color-surface-panel)", padding: "0.75rem", borderRadius: "0.5rem", fontSize: "0.85em", margin: "0.5rem 0", border: "1px solid rgba(148,163,184,0.06)", overflowX: "auto" }}>
            {tokens.map((line, i) => (
              <div key={i} {...getLineProps({ line })}>
                {line.map((token, key) => (
                  <span key={key} {...getTokenProps({ token })} />
                ))}
              </div>
            ))}
          </pre>
        )}
      </Highlight>
      <CopyButton text={text} className="absolute top-2 right-2 opacity-0 group-hover:opacity-100" label="Copy" />
    </div>
  );
}

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
        <div className="w-7 h-7 rounded-lg bg-accent-muted border border-accent-border flex items-center justify-center shrink-0 mr-2.5 mt-0.5">
          <svg className="w-3.5 h-3.5 text-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 2L3 7v5c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V7l-9-5z" />
            <path d="M9 12l2 2 4-4" />
          </svg>
        </div>
      )}

      <div className={`${isUser ? "max-w-[80%]" : "max-w-[calc(100%-40px)]"}`}>
        {isUser ? (
          <div className="px-4 py-2.5 rounded-2xl rounded-br-sm bg-teal-600 text-white text-sm leading-relaxed">
            {message.content}
          </div>
        ) : (
          <>
            {/* Workflow stepper */}
            {message.tools.length > 0 && (
              <div className="mb-2.5 p-3 bg-surface-card rounded-xl border border-edge">
                <div className="text-[11px] font-semibold uppercase tracking-wider text-fg-muted mb-1.5 flex items-center gap-1.5">
                  <span className="w-1 h-3.5 rounded-full bg-teal-400" />
                  Workflow
                </div>
                {message.tools.map((tool, i) => {
                  const isLast = i === message.tools.length - 1;
                  const isDone = !isLast || (isLast && message.content.length > 0);
                  return (
                    <div key={i} className="flex gap-2 relative">
                      {i < message.tools.length - 1 && (
                        <div className={`absolute left-[7px] top-[18px] bottom-[-2px] w-0.5 ${isDone ? "bg-emerald-500/50" : "bg-slate-700"}`} />
                      )}
                      <div className={`w-4 h-4 rounded-full flex items-center justify-center shrink-0 mt-0.5 text-[9px] font-bold ${isDone ? "bg-emerald-500/20 text-emerald-400 ring-1 ring-emerald-500/30" : "bg-teal-400/20 text-accent ring-1 ring-accent-border animate-pulse"}`}>
                        {isDone ? "\u2713" : i + 1}
                      </div>
                      <div className={`text-xs py-0.5 pb-2 ${isDone ? "text-fg-muted" : "text-accent font-medium"}`}>
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
                  <Markdown remarkPlugins={[remarkGfm]} components={{ table: CollapsibleTable, pre: CodeBlock }}>
                    {body}
                  </Markdown>
                </div>
                {isStreaming && <Spinner activeTool={message.tools[message.tools.length - 1]} />}
              </>
            ) : isStreaming ? (
              <div className="flex items-center gap-2.5 py-2">
                <Spinner startTime={message.timestamp} activeTool={message.tools[message.tools.length - 1]} />
              </div>
            ) : null}

            {/* Action buttons */}
            {showActions && onAction && (
              <div className="mt-3 p-3 bg-surface-card rounded-xl border border-edge">
                <div className="text-[11px] font-semibold uppercase tracking-wider text-fg-muted mb-2">
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
                        className="px-3 py-1.5 text-xs font-medium text-fg-secondary bg-input border border-edge rounded-lg hover:border-accent-border hover:text-accent hover:bg-accent-muted transition-all group"
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
              <div className="mt-2 flex items-center gap-1.5 text-red-400 text-sm">
                <ExclamationTriangleIcon className="w-4 h-4" />
                {message.error}
              </div>
            )}

            {/* Duration */}
            {message.durationMs != null && (
              <div className="text-[11px] text-fg-faint mt-2 font-mono">
                {(message.durationMs / 1000).toFixed(1)}s
              </div>
            )}
          </>
        )}
      </div>

      {/* Timestamp */}
      {!isStreaming && (
        <div className={`text-[10px] text-fg-faint self-end shrink-0 whitespace-nowrap font-mono ${isUser ? "mr-1" : "ml-1"}`}>
          {new Date(message.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
        </div>
      )}
    </div>
  );
}

function shortenAction(text: string): string {
  let s = text;
  s = s.replace(/\s*[-\u2013\u2014]\s*\d+\s+instances?.*$/i, "");
  s = s.replace(/\s*\([^)]*\d+[^)]*\)\s*$/, "");
  s = s.replace(/\s+(for|across)\s+all\s+\d*\s*environments?/i, "");
  if (s.length > 60) s = s.slice(0, 57) + "\u2026";
  return s.trim();
}

function Spinner({ startTime }: { startTime?: number; activeTool?: string }) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const start = startTime || Date.now();
    const timer = setInterval(() => setElapsed(Math.floor((Date.now() - start) / 1000)), 1000);
    return () => clearInterval(timer);
  }, [startTime]);

  const time = elapsed >= 60 ? `${Math.floor(elapsed / 60)}m ${elapsed % 60}s` : elapsed >= 5 ? `${elapsed}s` : "";
  const label = elapsed < 1 ? "Thinking..." : time ? `Working... ${time}` : "Working...";

  return (
    <div className="flex items-center gap-2 py-1">
      <div className="flex gap-1">
        <div className="w-1.5 h-1.5 bg-accent rounded-full animate-bounce [animation-delay:0ms]" />
        <div className="w-1.5 h-1.5 bg-accent rounded-full animate-bounce [animation-delay:150ms]" />
        <div className="w-1.5 h-1.5 bg-accent rounded-full animate-bounce [animation-delay:300ms]" />
      </div>
      <span className="text-[12px] text-fg-faint font-mono">{label}</span>
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
        className="text-xs font-semibold text-accent hover:text-accent-hover py-1.5 bg-transparent border-0 cursor-pointer"
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
    const cleaned = line.replace(/^[\s]*[-*\u2022]\s*/, "").replace(/^[\s]*\d+[.)]\s*/, "").replace(/[*_`]/g, "").replace(/^\s*>\s*/, "").trim();
    if (cleaned.length > 5 && cleaned.length < 120) actions.push(cleaned);
  }
  return { body, actions: actions.slice(0, 5) };
}
