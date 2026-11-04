import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { memo, useMemo, useState, useEffect, type ReactNode } from "react";
import { CopyButton } from "./ui/CopyButton";
import type { ChatMessage } from "../hooks/useAgentStream";
import { toolLabel } from "../lib/toolLabels";
import { clock, noEmDash, scanFollowUp, shortId } from "../lib/view";
import ResultCards from "./cards/ResultCards";
import type { Pane } from "../views/pane";

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
  return (
    <div className="relative group">
      <pre><code>{text}</code></pre>
      <CopyButton text={text} className="absolute top-1 right-1 opacity-0 group-hover:opacity-100 focus:opacity-100" label="Copy" />
    </div>
  );
}

interface Props {
  message: ChatMessage;
  isStreaming: boolean;
  onAction?: (text: string) => void;
  onOpen: (pane: Pane) => void;
  current?: Pane;
}

// Markdown plugins and renderers, made once so a re-render doesn't rebuild them.
const PLUGINS = [remarkGfm];
const RENDERERS = { table: CollapsibleTable, pre: CodeBlock, img: () => null };

/**
 * One chat message. Memoised: while a reply streams only its own bubble changes,
 * so earlier messages aren't re-rendered or their markdown parsed again.
 */
export default memo(function MessageBubble({ message, isStreaming, onAction, onOpen, current }: Props) {
  const isUser = message.role === "user";
  const { body, actions } = useMemo(() => {
    const cleaned = !isUser ? cleanAgentText(message.content) : "";
    return !isUser && cleaned ? extractActions(cleaned) : { body: cleaned, actions: [] as string[] };
  }, [isUser, message.content]);
  const showActions = !isStreaming && actions.length > 0 && !!onAction;
  const scan = useMemo(() => (isStreaming ? null : scanFollowUp(message)), [isStreaming, message]);
  const time = clock(message.timestamp);

  if (isUser) {
    return (
      <>
        <div className="msg me">{message.content}</div>
      </>
    );
  }
  return (
    <div className="msg bot animate-fade-in">
      {message.tools.length > 0 && (
        <ul className="steps" aria-label="What Patchy did">
          {message.tools.map((tool, i) => {
            const now = isStreaming && i === message.tools.length - 1 && !message.content;
            return <li key={i} className={now ? "now" : ""} title={tool}>{toolLabel(tool)}</li>;
          })}
        </ul>
      )}

      {message.cards && message.cards.length > 0 && (
        <div className="mb-2">
          <ResultCards cards={message.cards} onAction={!isStreaming ? onAction : undefined} onOpen={onOpen} current={current} />
        </div>
      )}

      {body ? (
        <div className="agent-markdown">
          <Markdown remarkPlugins={PLUGINS} components={RENDERERS}>
            {body}
          </Markdown>
        </div>
      ) : null}
      {isStreaming && <Spinner startTime={message.timestamp} activeTool={message.tools[message.tools.length - 1]} />}

      {showActions && (
        <div className="suggest" aria-label="Suggested next steps">
          {actions.map((action, i) => (
            <button key={i} type="button" className="btn small" onClick={() => onAction!(action)} title={action}>{shortenAction(action)}</button>
          ))}
        </div>
      )}

      {scan && (
        <div className="suggest" aria-label="Dry-run scan">
          <span className="d">Dry-run scan {scan.ids.map((id) => shortId(id, "run")).join(", ")} started. Scans take 2 to 3 minutes and install nothing.</span>
          {onAction && <button type="button" className="btn small" onClick={() => onAction(scan.prompt)} title={scan.prompt}>Show scan results</button>}
        </div>
      )}

      {message.error && <div className="risktxt mt-1" role="alert">{message.error}</div>}

      {!isStreaming && (
        <div className="stamp">{time}{message.durationMs != null ? `, ${(message.durationMs / 1000).toFixed(1)} s` : ""}</div>
      )}
    </div>
  );
});

function shortenAction(text: string): string {
  let s = text;
  s = s.replace(/\s*[-\u2013\u2014]\s*\d+\s+instances?.*$/i, "");
  s = s.replace(/\s*\([^)]*\d+[^)]*\)\s*$/, "");
  s = s.replace(/\s+(for|across)\s+all\s+\d*\s*environments?/i, "");
  if (s.length > 60) s = s.slice(0, 57) + "\u2026";
  return s.trim();
}

function Spinner({ startTime, activeTool }: { startTime?: number; activeTool?: string }) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const start = startTime || Date.now();
    const timer = setInterval(() => setElapsed(Math.floor((Date.now() - start) / 1000)), 1000);
    return () => clearInterval(timer);
  }, [startTime]);

  const time = elapsed >= 60 ? `${Math.floor(elapsed / 60)} min ${elapsed % 60} s` : elapsed >= 5 ? `${elapsed} s` : "";
  const step = activeTool ? toolLabel(activeTool) : "";
  const label = step ? `${step}${time ? `, ${time}` : ""}` : time ? `Working, ${time}` : "Thinking";

  return <div className="working" role="status">{label}</div>;
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
        className="link text-[13px] py-1"
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
  return noEmDash(text.trim());
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
