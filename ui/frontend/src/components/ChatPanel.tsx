import { useState, useRef, useEffect, useCallback } from "react";
import { PaperAirplaneIcon, StopIcon } from "@heroicons/react/24/solid";
import { LockClosedIcon } from "@heroicons/react/24/outline";
import MessageBubble from "./MessageBubble";
import type { ChatMessage } from "../hooks/useAgentStream";
import type { Role } from "../hooks/useRole";

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
  { icon: "🔍", label: "Discover vulnerabilities", text: "What critical vulnerabilities do we have?", desc: "Scan fleet for active CVEs" },
  { icon: "🖥️", label: "Fleet status", text: "Show EC2 instances in dev environment", desc: "Check instance health and SSM status" },
  { icon: "📋", label: "Patch compliance", text: "Show me patch compliance status", desc: "Missing patches and compliance gaps" },
  { icon: "⚡", label: "Remediate a CVE", text: "Handle CVE-2025-38527 in dev", desc: "End-to-end patch workflow" },
];

export default function ChatPanel({
  messages, isStreaming, onSend, onStop, onClear,
  prefillText, onPrefillConsumed, role = "operator",
}: Props) {
  const [input, setInput] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const isViewer = role === "viewer";

  const autoResize = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 96) + "px"; // max ~3 lines
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    if (prefillText) {
      setInput(prefillText);
      onPrefillConsumed?.();
      textareaRef.current?.focus();
      // Trigger resize after state update
      setTimeout(autoResize, 0);
    }
  }, [prefillText, onPrefillConsumed, autoResize]);

  const handleSend = () => {
    const text = input.trim();
    if (!text || isStreaming) return;
    setInput("");
    // Reset textarea height
    if (textareaRef.current) textareaRef.current.style.height = "auto";
    onSend(text);
  };

  return (
    <div className="flex flex-col h-full">
      {/* Scrollable messages */}
      <div className="flex-1 overflow-y-auto px-3 py-3 min-h-0 custom-scrollbar">
        {messages.length === 0 ? (
          <EmptyState onSelect={(text) => setInput(text)} />
        ) : (
          messages.map((msg) => (
            <MessageBubble
              key={msg.id}
              message={msg}
              isStreaming={isStreaming && msg.id === messages[messages.length - 1]?.id}
              onAction={!isStreaming ? onSend : undefined}
            />
          ))
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input bar */}
      <div className="shrink-0 px-3 py-2 border-t border-gray-200 bg-white">
        {isViewer ? (
          <div className="flex items-center justify-center gap-2 py-2 text-sm text-gray-500">
            <LockClosedIcon className="w-4 h-4" />
            Read-only mode — viewer role cannot send messages.
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
                  disabled={isStreaming}
                  autoFocus
                  rows={1}
                  className="w-full px-3 py-2 pr-8 text-sm border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent disabled:bg-gray-50 disabled:text-gray-400 placeholder:text-gray-400 resize-none overflow-hidden leading-relaxed"
                />
                {!input.trim() && !isStreaming && (
                  <span className="absolute right-3 bottom-2.5 text-[10px] text-gray-300 pointer-events-none select-none">↵</span>
                )}
              </div>
              {isStreaming ? (
                <button onClick={onStop} className="px-3 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50 transition flex items-center gap-1.5">
                  <StopIcon className="w-4 h-4" /> Stop
                </button>
              ) : (
                <button
                  onClick={handleSend}
                  disabled={!input.trim()}
                  className="px-3 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1.5"
                >
                  <PaperAirplaneIcon className="w-4 h-4" /> Send
                </button>
              )}
            </div>
            {messages.length > 0 && (
              <div className="flex justify-end mt-1">
                <button
                  onClick={onClear}
                  disabled={isStreaming}
                  className="text-xs text-blue-600 hover:text-blue-800 disabled:text-gray-400 transition"
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

function EmptyState({ onSelect }: { onSelect: (text: string) => void }) {
  return (
    <div className="flex flex-col items-center pt-16 px-4">
      <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-blue-500 to-cyan-400 flex items-center justify-center text-3xl mb-4 shadow-lg shadow-blue-500/20">
        🛡️
      </div>
      <h3 className="text-xl font-bold text-gray-900">What can I help you with?</h3>
      <p className="text-sm text-gray-500 mt-1.5 mb-8 text-center max-w-sm">
        Ask about vulnerabilities, patching, or compliance — or pick a starting point below.
      </p>
      <div className="grid grid-cols-2 gap-3 max-w-lg w-full">
        {EXAMPLE_PROMPTS.map((prompt) => (
          <button
            key={prompt.text}
            onClick={() => onSelect(prompt.text)}
            className="text-left p-4 rounded-xl border border-gray-200 bg-white hover:border-blue-300 hover:shadow-md hover:-translate-y-0.5 transition-all duration-200 group cursor-pointer"
            aria-label={prompt.label}
          >
            <div className="text-xl mb-1.5">{prompt.icon}</div>
            <div className="text-sm font-semibold text-gray-900 group-hover:text-blue-700 transition">{prompt.label}</div>
            <div className="text-xs text-gray-500 mt-1 leading-relaxed">{prompt.desc}</div>
          </button>
        ))}
      </div>
    </div>
  );
}
