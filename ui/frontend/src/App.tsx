import { useState, useCallback, useEffect } from "react";
import {
  ChevronDoubleLeftIcon,
  ChevronDoubleRightIcon,
  ArrowTopRightOnSquareIcon,
  ArrowRightStartOnRectangleIcon,
} from "@heroicons/react/24/outline";
import { Menu, MenuButton, MenuItem, MenuItems } from "@headlessui/react";
import ChatPanel from "./components/ChatPanel";
import DashboardPanel from "./components/DashboardPanel";
import { CommandPalette } from "./components/CommandPalette";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { useToast, ToastContainer } from "./components/ui/Toast";
import { useAgentStream } from "./hooks/useAgentStream";
import { useRole } from "./hooks/useRole";
import { useAuth } from "./hooks/useAuth";
import { useTheme } from "./hooks/useTheme";
import { useIdleLogout } from "./hooks/useIdleLogout";

function ShieldIcon({ className = "" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 2L3 7v5c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V7l-9-5z" />
      <path d="M9 12l2 2 4-4" />
    </svg>
  );
}

export default function App() {
  const { role, setRole } = useRole();
  const auth = useAuth();
  const { messages, isStreaming, sessionId, sendMessage, stopStreaming, clearMessages } =
    useAgentStream(role);
  const { toasts, toast, dismiss } = useToast();
  const { theme, toggle: toggleTheme } = useTheme();
  const [chatPrefill, setChatPrefill] = useState("");
  const [dashboardOpen, setDashboardOpen] = useState(true);
  const [chatHighlight, setChatHighlight] = useState(false);
  const [cmdPaletteOpen, setCmdPaletteOpen] = useState(false);

  // Toast on copy and new conversation
  const handleClearMessages = useCallback(() => {
    clearMessages();
    toast("New conversation started", "info");
  }, [clearMessages, toast]);

  // Auto sign-out after 60 minutes of inactivity. Closes the gap left by the
  // ALB's 7-day session cookie default — a user who walks away from their
  // desk stays signed in unless we client-side-redirect them.
  useIdleLogout(auth?.logoutUrl, 60 * 60 * 1000);

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setCmdPaletteOpen((v) => !v);
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, []);

  // Keyboard shortcuts for power users
  useEffect(() => {
    const handleShortcut = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement).tagName;
      if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;

      if (e.key === "/") {
        e.preventDefault();
        document.dispatchEvent(new Event("patchy:focus-chat"));
      } else if (e.key === "r") {
        document.dispatchEvent(new Event("patchy:refresh"));
      } else if (e.key >= "1" && e.key <= "5") {
        document.dispatchEvent(new CustomEvent("patchy:tab", { detail: parseInt(e.key) }));
      }
    };
    document.addEventListener("keydown", handleShortcut);
    return () => document.removeEventListener("keydown", handleShortcut);
  }, []);

  const handleChatPrefill = useCallback((text: string) => {
    setChatPrefill(text);
    setChatHighlight(true);
    setTimeout(() => setChatHighlight(false), 1200);
  }, []);

  return (
    <div className="flex flex-col h-screen overflow-hidden bg-surface-base font-sans">
      {/* ── Header ──────────────────────────────────────────────── */}
      <header className="flex items-center justify-between px-5 h-14 bg-surface-panel shrink-0 relative">
        <div className="absolute bottom-0 left-0 right-0 accent-line" />

        <a href="/" className="flex items-center gap-2.5 text-fg no-underline">
          <div className="w-8 h-8 rounded-lg bg-accent-muted border border-accent-border flex items-center justify-center">
            <ShieldIcon className="w-[18px] h-[18px] text-accent" />
          </div>
          <div className="flex items-baseline gap-2">
            <span className="font-bold text-[15px] tracking-tight text-fg">Patchy</span>
            <span className="text-[11px] text-fg-muted hidden sm:inline">Intelligent Patch Automation</span>
          </div>
        </a>

        <div className="flex items-center gap-3">
          {auth?.email && (
            <span className="text-[11px] text-fg-muted hidden sm:inline font-mono">
              {auth.email}
            </span>
          )}
          <span className="text-[11px] text-fg-faint hidden sm:inline font-mono">
            {sessionId.slice(0, 12)}
          </span>

          {/* Role switcher */}
          <Menu as="div" className="relative">
            <MenuButton className="flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg bg-hover hover:bg-hover transition text-fg-secondary cursor-pointer border border-edge">
              <span className={`w-1.5 h-1.5 rounded-full ${role === "operator" ? "bg-teal-400" : "bg-slate-500"}`} />
              {role === "operator" ? "Operator" : "Viewer"}
              <svg className="w-3 h-3 text-fg-muted" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
            </MenuButton>
            <MenuItems className="absolute right-0 mt-1.5 w-52 bg-surface-raised rounded-xl shadow-xl ring-1 ring-white/[0.1] py-1 z-50">
              <MenuItem>
                <button onClick={() => setRole("operator")} className={`w-full text-left px-3 py-2.5 text-sm hover:bg-hover transition flex items-center gap-2.5 ${role === "operator" ? "text-accent font-medium" : "text-fg-secondary"}`}>
                  <span className="w-1.5 h-1.5 rounded-full bg-teal-400" />
                  Operator &mdash; full access
                </button>
              </MenuItem>
              <MenuItem>
                <button onClick={() => setRole("viewer")} className={`w-full text-left px-3 py-2.5 text-sm hover:bg-hover transition flex items-center gap-2.5 ${role === "viewer" ? "text-accent font-medium" : "text-fg-secondary"}`}>
                  <span className="w-1.5 h-1.5 rounded-full bg-slate-500" />
                  Viewer &mdash; read-only
                </button>
              </MenuItem>
            </MenuItems>
          </Menu>

          {auth?.logoutUrl && (
            <button
              onClick={() => {
                // Clear session keys so the next sign-in starts fresh.
                // useAgentStream also detects user-mismatch via the
                // patchy-session-user key, but clearing here makes the
                // intent explicit (sign-out always = new chat).
                localStorage.removeItem("patchy-session-id");
                localStorage.removeItem("patchy-session-user");
                window.location.replace(auth.logoutUrl!);
              }}
              className="flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg bg-hover hover:bg-red-500/10 hover:text-red-400 transition text-fg-muted cursor-pointer border border-edge"
              title="Sign out"
            >
              <ArrowRightStartOnRectangleIcon className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">Sign out</span>
            </button>
          )}

          <button
            onClick={() => setCmdPaletteOpen(true)}
            className="hidden sm:flex items-center gap-1.5 text-[11px] text-fg-muted hover:text-fg-secondary bg-input border border-edge rounded-lg px-2 py-1 transition"
            title="Command palette"
          >
            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" /></svg>
            <kbd className="font-mono text-[10px] text-fg-faint">{navigator.platform?.includes("Mac") ? "\u2318" : "Ctrl+"}K</kbd>
          </button>

          <button
            onClick={toggleTheme}
            className="p-1.5 rounded-lg bg-input border border-edge hover:bg-hover text-fg-muted hover:text-fg transition"
            title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
          >
            {theme === "dark" ? (
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M12 3v2.25m6.364.386l-1.591 1.591M21 12h-2.25m-.386 6.364l-1.591-1.591M12 18.75V21m-4.773-4.227l-1.591 1.591M5.25 12H3m4.227-4.773L5.636 5.636M15.75 12a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0z" /></svg>
            ) : (
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M21.752 15.002A9.718 9.718 0 0118 15.75c-5.385 0-9.75-4.365-9.75-9.75 0-1.33.266-2.597.748-3.752A9.753 9.753 0 003 11.25C3 16.635 7.365 21 12.75 21a9.753 9.753 0 009.002-5.998z" /></svg>
            )}
          </button>

          <a
            href="https://github.com/aws-samples"
            target="_blank"
            rel="noopener noreferrer"
            className="text-fg-faint hover:text-fg-muted transition"
            aria-label="Help (opens in new tab)"
          >
            <ArrowTopRightOnSquareIcon className="w-4 h-4" />
          </a>
        </div>
      </header>

      <CommandPalette open={cmdPaletteOpen} onClose={() => setCmdPaletteOpen(false)} onSend={(text) => { sendMessage(text); setCmdPaletteOpen(false); }} />

      {/* ── Main content ────────────────────────────────────────── */}
      <div className="flex flex-1 overflow-hidden relative">
        {/* Dashboard panel — overlay on mobile, side panel on desktop */}
        {dashboardOpen && (
          <>
            <div className="md:hidden fixed inset-0 bg-black/50 z-30" onClick={() => setDashboardOpen(false)} />
            <div className="fixed md:relative z-40 md:z-auto inset-y-14 left-0 w-[85%] md:w-[72%] md:inset-auto min-w-0 md:min-w-[500px] border-r border-edge overflow-hidden flex flex-col bg-surface-panel">
            <div className="flex items-center justify-between px-5 pt-4 pb-2 shrink-0">
              <h2 className="text-base font-semibold text-fg">Dashboard</h2>
              <button
                onClick={() => setDashboardOpen(false)}
                className="p-1.5 rounded-lg hover:bg-hover text-fg-muted hover:text-fg-secondary transition"
                aria-label="Collapse dashboard"
              >
                <ChevronDoubleLeftIcon className="w-4 h-4" />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto px-5 pb-4 custom-scrollbar">
              <ErrorBoundary>
                <DashboardPanel onChatPrefill={handleChatPrefill} role={role} />
              </ErrorBoundary>
            </div>
          </div>
          </>
        )}

        {/* Chat panel */}
        <div className="flex-1 flex flex-col overflow-hidden min-w-0 bg-surface-base">
          <div className={`flex items-center gap-2.5 px-4 py-2.5 border-b border-edge bg-surface-panel shrink-0 ${chatHighlight ? "chat-highlight" : ""}`}>
            {!dashboardOpen && (
              <button
                onClick={() => setDashboardOpen(true)}
                className="p-1.5 rounded-lg hover:bg-hover text-fg-muted hover:text-fg-secondary transition"
                aria-label="Show dashboard"
              >
                <ChevronDoubleRightIcon className="w-4 h-4" />
              </button>
            )}
            <div className="w-6 h-6 rounded-md bg-accent-muted flex items-center justify-center">
              <ShieldIcon className="w-3.5 h-3.5 text-accent" />
            </div>
            <span className="font-semibold text-sm text-fg">Patchy</span>
          </div>

          <div className="flex-1 overflow-hidden">
            <ErrorBoundary>
              <ChatPanel
                messages={messages}
                isStreaming={isStreaming}
                onSend={sendMessage}
                onStop={stopStreaming}
                onClear={handleClearMessages}
                sessionId={sessionId}
                prefillText={chatPrefill}
                onPrefillConsumed={() => setChatPrefill("")}
                role={role}
              />
            </ErrorBoundary>
          </div>
        </div>
      </div>
      <ToastContainer toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
