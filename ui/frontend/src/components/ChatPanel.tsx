import { useState, useRef, useEffect, useCallback } from "react";
import MessageBubble from "./MessageBubble";
import type { ChatMessage } from "../hooks/useAgentStream";
import type { Role } from "../hooks/useRole";
import { isReadOnlyPrompt } from "../lib/actions";
import { nearBottom } from "../lib/view";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import type { Pane } from "../views/pane";

interface Props {
  messages: ChatMessage[];
  isStreaming: boolean;
  /** False while an approve or stop streams: cutting those off would lose their outcome. */
  canStopReply?: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
  onClear: () => void;
  /** The conversation's history is still loading. */
  restoring?: boolean;
  prefillText?: string;
  onPrefillConsumed?: () => void;
  role?: Role;
  /** False until the server has said what the user's role is. */
  roleKnown?: boolean;
  onOpen: (pane: Pane) => void;
  current?: Pane;
}

const STARTERS = [
  { text: "What critical vulnerabilities do we have?", desc: "Findings across the fleet, by severity" },
  { text: "Show patch compliance status for dev", desc: "Missing patches and compliance gaps" },
  { text: "Which findings in prod will miss their SLA?", desc: "Deadlines against the next maintenance window" },
  { text: "Patch HIGH and above in dev", desc: "Prepares a change for you to review and approve" },
];

export default function ChatPanel({
  messages, isStreaming, canStopReply = true, onSend, onStop, onClear, restoring = false, prefillText, onPrefillConsumed, role = "viewer", roleKnown = true,
  onOpen, current,
}: Props) {
  const [input, setInput] = useState("");
  const [confirmClear, setConfirmClear] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);
  // Follow new text only while the reader is at the end; scrolling up to read stops it.
  const stick = useRef(true);
  const count = useRef(0);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const isViewer = roleKnown && role === "viewer";
  const wasStreaming = useRef(false);

  // The box is disabled while a reply streams; give focus back when it ends.
  useEffect(() => {
    if (wasStreaming.current && !isStreaming && document.activeElement === document.body) textareaRef.current?.focus();
    wasStreaming.current = isStreaming;
  }, [isStreaming]);

  useEffect(() => {
    const el = logRef.current;
    // Something the user just sent always shows.
    if (messages.length > count.current && messages.slice(count.current).some((m) => m.role === "user")) stick.current = true;
    count.current = messages.length;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const onScroll = useCallback(() => {
    const el = logRef.current;
    if (el) stick.current = nearBottom(el.scrollTop, el.clientHeight, el.scrollHeight);
  }, []);

  // Card buttons: a read sends at once; anything that could change something
  // fills the chat box, so the operator reads the prompt and sends it themselves.
  const handleAction = useCallback((text: string) => {
    if (isReadOnlyPrompt(text)) {
      onSend(text);
      return;
    }
    setInput(text);
    textareaRef.current?.focus();
  }, [onSend]);

  useEffect(() => {
    if (prefillText) {
      setInput(prefillText);
      onPrefillConsumed?.();
      textareaRef.current?.focus();
    }
  }, [prefillText, onPrefillConsumed]);

  useEffect(() => {
    const focus = () => textareaRef.current?.focus();
    document.addEventListener("patchy:focus-chat", focus);
    return () => document.removeEventListener("patchy:focus-chat", focus);
  }, []);

  const handleSend = () => {
    const text = input.trim();
    if (!text || isStreaming) return;
    setInput("");
    onSend(text);
  };

  return (
    <section className="chatcol" aria-label="Chat with Patchy">
      <div className="head">
        <div><strong>Patchy</strong><span className="d">Plans changes; you approve them</span></div>
        {messages.length > 0 && !isViewer && (
          <button type="button" className="btn quiet small" onClick={() => setConfirmClear(true)} disabled={isStreaming}>New conversation</button>
        )}
      </div>
      <div className="chat custom-scrollbar" role="log" aria-live="polite" aria-busy={isStreaming} ref={logRef} onScroll={onScroll}>
        {messages.length === 0 && restoring ? (
          <div className="d" role="status">Loading conversation…</div>
        ) : messages.length === 0 && roleKnown ? (
          <div className="d">
            {isViewer ? (
              <p className="m-0">You have read-only access. The overview, findings and reports are on the right; operators chat with Patchy and approve changes.</p>
            ) : (
              <>
                <p className="m-0">Ask about findings, patching or compliance. When you ask for a change, Patchy prepares it and opens it on the right for you to review; nothing runs until you approve it there.</p>
                <div className="starters">
                  {STARTERS.map((s) => (
                    <button key={s.text} type="button" onClick={() => { setInput(s.text); textareaRef.current?.focus(); }}>
                      {s.text}<span className="d">{s.desc}</span>
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>
        ) : (
          messages.map((msg) => (
            <MessageBubble
              key={msg.id}
              message={msg}
              isStreaming={isStreaming && msg.id === messages[messages.length - 1]?.id}
              onAction={!isStreaming && !isViewer ? handleAction : undefined}
              onOpen={onOpen}
              current={current}
            />
          ))
        )}
      </div>

      {roleKnown && !isViewer && (
        <div className="compose">
          <label htmlFor="patchy-ask" className="sr-only">Message Patchy</label>
          <textarea
            id="patchy-ask"
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); } }}
            placeholder="Ask about findings, patching or compliance"
            aria-label="Chat message input"
            disabled={isStreaming}
          />
          <div className="row">
            <span>Enter to send, Shift+Enter for a new line</span>
            <span className="btns">
              {isStreaming ? (
                canStopReply ? <button type="button" className="btn" onClick={onStop}>Stop reply</button>
                  : <button type="button" className="btn" disabled>Working…</button>
              ) : (
                <button type="button" className="btn primary" onClick={handleSend} disabled={!input.trim()}>Send</button>
              )}
            </span>
          </div>
        </div>
      )}
      <ConfirmDialog
        open={confirmClear}
        title="Start a new conversation?"
        description="The chat and its list of changes are cleared, and plans still waiting can't be approved afterwards. Runs already started carry on; their reports appear in Reports."
        confirmLabel="New conversation"
        cancelLabel="Keep this one"
        onConfirm={() => { setConfirmClear(false); onClear(); }}
        onCancel={() => setConfirmClear(false)}
      />
    </section>
  );
}
