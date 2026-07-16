import { useState, useRef, useEffect, useCallback } from "react";
import { PaperAirplaneIcon, StopIcon } from "@heroicons/react/24/solid";
import { LockClosedIcon, ChevronDownIcon } from "@heroicons/react/24/outline";
import MessageBubble from "./MessageBubble";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import type { ChatMessage } from "../hooks/useAgentStream";
import type { Role } from "../hooks/useRole";

const DESTRUCTIVE_PATTERN = /(^(start|proceed|begin|run)\b|rollback|^patch\b)/i;

interface Props {
  messages: ChatMessage[];
  isStreaming: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
  onClear: () => void;
  sessionId: string;
  prefillText?: string;
  onPrefillConsumed?: () => void;
  role?: Role;
}

const EXAMPLE_PROMPTS = [
  { icon: "search", label: "Discover vulnerabilities", text: "What critical vulnerabilities do we have?", desc: "Scan fleet for active CVEs" },
  { icon: "server", label: "Fleet status", text: "Show EC2 instances in dev environment", desc: "Check instance health and SSM status" },
  { icon: "clipboard", label: "Patch compliance", text: "Show me patch compliance status", desc: "Missing patches and compliance gaps" },
  { icon: "bolt", label: "Remediate a CVE", text: "Handle CVE-2025-38527 in dev", desc: "End-to-end patch workflow" },
];

const PROMPT_ICONS: Record<string, JSX.Element> = {
  search: <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" /></svg>,
  server: <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M5.25 14.25h13.5m-13.5 0a3 3 0 01-3-3m3 3a3 3 0 100 6h13.5a3 3 0 100-6m-16.5-3a3 3 0 013-3h13.5a3 3 0 013 3m-19.5 0a4.5 4.5 0 01.9-2.7L5.737 5.1a3.375 3.375 0 012.7-1.35h7.126c1.062 0 2.062.5 2.7 1.35l2.587 3.45a4.5 4.5 0 01.9 2.7m0 0a3 3 0 01-3 3m0 3h.008v.008h-.008v-.008zm0-6h.008v.008h-.008v-.008zm-3 6h.008v.008h-.008v-.008zm0-6h.008v.008h-.008v-.008z" /></svg>,
  clipboard: <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M9 12h3.75M9 15h3.75M9 18h3.75m3 .75H18a2.25 2.25 0 002.25-2.25V6.108c0-1.135-.845-2.098-1.976-2.192a48.424 48.424 0 00-1.123-.08m-5.801 0c-.065.21-.1.433-.1.664 0 .414.336.75.75.75h4.5a.75.75 0 00.75-.75 2.25 2.25 0 00-.1-.664m-5.8 0A2.251 2.251 0 0113.5 2.25H15c1.012 0 1.867.668 2.15 1.586m-5.8 0c-.376.023-.75.05-1.124.08C9.095 4.01 8.25 4.973 8.25 6.108V8.25m0 0H4.875c-.621 0-1.125.504-1.125 1.125v11.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125V9.375c0-.621-.504-1.125-1.125-1.125H8.25z" /></svg>,
  bolt: <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z" /></svg>,
};

export default function ChatPanel({
  messages, isStreaming, onSend, onStop, onClear,
  prefillText, onPrefillConsumed, role = "operator",
}: Props) {
  const [input, setInput] = useState("");
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [showScrollBtn, setShowScrollBtn] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const isViewer = role === "viewer";

  const autoResize = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 96) + "px";
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setShowScrollBtn(distanceFromBottom > 200);
  }, []);

  const handleActionWithConfirm = useCallback((text: string) => {
    if (DESTRUCTIVE_PATTERN.test(text)) {
      setPendingAction(text);
    } else {
      onSend(text);
    }
  }, [onSend]);

  useEffect(() => {
    if (prefillText) {
      setInput(prefillText);
      onPrefillConsumed?.();
      textareaRef.current?.focus();
      setTimeout(autoResize, 0);
    }
  }, [prefillText, onPrefillConsumed, autoResize]);

  // Listen for keyboard shortcut to focus chat input
  useEffect(() => {
    const handleFocus = () => textareaRef.current?.focus();
    document.addEventListener("patchy:focus-chat", handleFocus);
    return () => document.removeEventListener("patchy:focus-chat", handleFocus);
  }, []);

  const handleSend = () => {
    const text = input.trim();
    if (!text || isStreaming) return;
    setInput("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
    onSend(text);
  };

  return (
    <div className="flex flex-col h-full">
      {/* Confirmation dialog for destructive actions */}
      <ConfirmDialog
        open={pendingAction !== null}
        title="Confirm Operation"
        description={`You are about to execute: "${pendingAction}". This action will modify your infrastructure. Continue?`}
        confirmLabel="Execute"
        cancelLabel="Cancel"
        variant="destructive"
        onConfirm={() => { if (pendingAction) onSend(pendingAction); setPendingAction(null); }}
        onCancel={() => setPendingAction(null)}
      />

      {/* Scrollable messages */}
      <div ref={scrollRef} onScroll={handleScroll} className="flex-1 overflow-y-auto px-4 py-4 min-h-0 custom-scrollbar relative">
        {messages.length === 0 ? (
          <EmptyState
            onSelect={(text) => setInput(text)}
          />
        ) : (
          messages.map((msg) => (
            <MessageBubble
              key={msg.id}
              message={msg}
              isStreaming={isStreaming && msg.id === messages[messages.length - 1]?.id}
              onAction={!isStreaming ? handleActionWithConfirm : undefined}
            />
          ))
        )}
        <div ref={bottomRef} />

        {/* Scroll to bottom */}
        {showScrollBtn && (
          <button
            onClick={() => bottomRef.current?.scrollIntoView({ behavior: "smooth" })}
            className="sticky bottom-2 left-1/2 -translate-x-1/2 w-8 h-8 rounded-full bg-surface-card border border-edge-strong flex items-center justify-center text-fg-muted hover:text-accent hover:border-accent-border transition shadow-lg z-10"
            aria-label="Scroll to bottom"
          >
            <ChevronDownIcon className="w-4 h-4" />
          </button>
        )}
      </div>

      {/* Input bar */}
      <div className="shrink-0 px-4 py-3 border-t border-edge bg-surface-panel">
        {isViewer ? (
          <div className="flex items-center justify-center gap-2 py-2 text-sm text-fg-muted">
            <LockClosedIcon className="w-4 h-4" />
            Read-only mode &mdash; viewer role cannot send messages.
          </div>
        ) : (
          <>
            <div className="flex gap-2 items-end">
              <div className="flex-1 relative">
                <textarea
                  ref={textareaRef}
                  value={input}
                  onChange={(e) => { setInput(e.target.value); autoResize(); }}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); } }}
                  placeholder="Ask about vulnerabilities, patching, compliance..."
                  aria-label="Chat message input"
                  disabled={isStreaming}
                  autoFocus
                  rows={1}
                  className="w-full px-3.5 py-2.5 pr-8 text-sm bg-input border border-edge rounded-xl text-fg placeholder:text-fg-faint focus:outline-none focus:ring-2 focus:ring-accent-border focus:border-accent-border disabled:opacity-40 resize-none overflow-hidden leading-relaxed transition"
                />
                {!input.trim() && !isStreaming && (
                  <span className="absolute right-3 bottom-2.5 text-[10px] text-fg-faint pointer-events-none select-none font-mono">Enter</span>
                )}
              </div>
              {isStreaming ? (
                <button onClick={onStop} className="px-3.5 py-2.5 text-sm font-medium text-fg-secondary bg-hover border border-edge rounded-xl hover:bg-hover transition flex items-center gap-1.5">
                  <StopIcon className="w-4 h-4" /> Stop
                </button>
              ) : (
                <button
                  onClick={handleSend}
                  disabled={!input.trim()}
                  className="px-3.5 py-2.5 text-sm font-medium text-surface-base bg-accent rounded-xl hover:bg-teal-300 transition disabled:opacity-30 disabled:cursor-not-allowed flex items-center gap-1.5"
                >
                  <PaperAirplaneIcon className="w-4 h-4" /> Send
                </button>
              )}
            </div>
            {messages.length > 0 && (
              <div className="flex justify-end mt-1.5">
                <button
                  onClick={onClear}
                  disabled={isStreaming}
                  className="text-xs text-accent/70 hover:text-accent disabled:text-fg-faint transition"
                >
                  New conversation
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function EmptyState({
  onSelect,
}: {
  onSelect: (text: string) => void;
}) {
  return (
    <div className="flex flex-col items-center pt-16 px-4">
      <div className="w-16 h-16 rounded-2xl bg-accent-muted border border-accent-border flex items-center justify-center mb-5 animate-glow">
        <svg className="w-8 h-8 text-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 2L3 7v5c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V7l-9-5z" />
          <path d="M9 12l2 2 4-4" />
        </svg>
      </div>
      <h3 className="text-xl font-bold text-fg">What can I help you with?</h3>
      <p className="text-sm text-fg-muted mt-2 mb-8 text-center max-w-sm">
        Ask about vulnerabilities, patching, or compliance &mdash; or pick a starting point below.
      </p>
      <div className="grid grid-cols-2 gap-3 max-w-lg w-full">
        {EXAMPLE_PROMPTS.map((prompt, i) => (
          <button
            key={prompt.text}
            onClick={() => onSelect(prompt.text)}
            className={`text-left p-4 rounded-xl border border-edge bg-surface-card hover:border-accent-border hover:bg-surface-raised hover:-translate-y-0.5 transition-all duration-200 group cursor-pointer animate-slide-up stagger-${i + 1}`}
            aria-label={prompt.label}
          >
            <div className="text-accent/70 mb-2 group-hover:text-accent transition">{PROMPT_ICONS[prompt.icon]}</div>
            <div className="text-sm font-semibold text-fg group-hover:text-accent-hover transition">{prompt.label}</div>
            <div className="text-xs text-fg-muted mt-1 leading-relaxed">{prompt.desc}</div>
          </button>
        ))}
      </div>
    </div>
  );
}
