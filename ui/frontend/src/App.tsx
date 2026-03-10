import { useState, useCallback, lazy, Suspense } from "react";
import {
  ShieldCheckIcon,
  ChevronDoubleLeftIcon,
  ChevronDoubleRightIcon,
  ArrowTopRightOnSquareIcon,
  ArrowRightStartOnRectangleIcon,
} from "@heroicons/react/24/outline";
import { Menu, MenuButton, MenuItem, MenuItems } from "@headlessui/react";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { useAgentStream } from "./hooks/useAgentStream";
import { useRole } from "./hooks/useRole";
import { useAuth } from "./hooks/useAuth";

const ChatPanel = lazy(() => import("./components/ChatPanel"));
const DashboardPanel = lazy(() => import("./components/DashboardPanel"));

function PanelFallback() {
  return <div className="flex items-center justify-center h-full text-gray-400 text-sm">Loading...</div>;
}

export default function App() {
  const { role, setRole } = useRole();
  const { auth, authError } = useAuth();
  const { messages, isStreaming, sessionId, sendMessage, stopStreaming, clearMessages } =
    useAgentStream(role);
  const [chatPrefill, setChatPrefill] = useState("");
  const [dashboardOpen, setDashboardOpen] = useState(true);
  const [chatHighlight, setChatHighlight] = useState(false);

  const handleChatPrefill = useCallback((text: string) => {
    setChatPrefill(text);
    setChatHighlight(true);
    setTimeout(() => setChatHighlight(false), 1200);
  }, []);

  return (
    <div className="flex flex-col h-screen overflow-hidden bg-white font-sans">
      {/* Top Navigation */}
      <header className="flex items-center justify-between px-4 h-12 bg-gray-900 text-white shrink-0">
        <a href="/" className="flex items-center gap-2 text-white no-underline">
          <ShieldCheckIcon className="w-5 h-5 text-blue-400" />
          <span className="font-semibold text-sm">Patchy — Intelligent Patch Automation</span>
        </a>
        <div className="flex items-center gap-3">
          {auth?.email && (
            <span className="text-xs text-gray-400 hidden sm:inline">
              {auth.email}
            </span>
          )}
          {authError && (
            <span className="text-xs text-red-400 hidden sm:inline" title={authError}>
              Auth error
            </span>
          )}
          <span className="text-xs text-gray-400 hidden md:inline">
            Session: {sessionId.slice(0, 12)}…
          </span>

          {/* Role switcher */}
          <Menu as="div" className="relative">
            <MenuButton className="flex items-center gap-1 text-xs px-2.5 py-1 rounded-md bg-gray-800 hover:bg-gray-700 transition text-gray-200 cursor-pointer border-0">
              <span aria-hidden="true">{role === "operator" ? "\uD83D\uDC64" : "\uD83D\uDC41\uFE0F"}</span>
              {" "}{role === "operator" ? "Operator" : "Viewer"}
              <svg className="w-3 h-3 ml-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
            </MenuButton>
            <MenuItems className="absolute right-0 mt-1 w-48 bg-white rounded-lg shadow-lg ring-1 ring-black/5 py-1 z-50">
              <MenuItem>
                <button onClick={() => setRole("operator")} className={`w-full text-left px-3 py-2 text-sm hover:bg-gray-50 ${role === "operator" ? "text-blue-600 font-medium" : "text-gray-700"}`}>
                  <span aria-hidden="true">{"\uD83D\uDC64"}</span> Operator — full access
                </button>
              </MenuItem>
              <MenuItem>
                <button onClick={() => setRole("viewer")} className={`w-full text-left px-3 py-2 text-sm hover:bg-gray-50 ${role === "viewer" ? "text-blue-600 font-medium" : "text-gray-700"}`}>
                  <span aria-hidden="true">{"\uD83D\uDC41\uFE0F"}</span> Viewer — read-only
                </button>
              </MenuItem>
            </MenuItems>
          </Menu>

          {auth?.logoutUrl && (
            <button
              onClick={() => {
                localStorage.removeItem("session_id");
                window.location.replace(auth.logoutUrl!);
              }}
              className="flex items-center gap-1 text-xs px-2.5 py-1 rounded-md bg-gray-800 hover:bg-red-700 transition text-gray-200 cursor-pointer border-0"
              title="Sign out"
            >
              <ArrowRightStartOnRectangleIcon className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">Sign out</span>
            </button>
          )}

          <a
            href="https://github.com/aws-samples"
            target="_blank"
            rel="noopener noreferrer"
            className="text-gray-400 hover:text-white transition"
            aria-label="Help (opens in new tab)"
          >
            <ArrowTopRightOnSquareIcon className="w-4 h-4" />
          </a>
        </div>
      </header>

      {/* Main content area */}
      <div className="flex flex-1 overflow-hidden">
        {/* Dashboard panel — collapsible */}
        {dashboardOpen && (
          <div className="w-[72%] min-w-[500px] border-r border-gray-200 overflow-hidden flex flex-col">
            <div className="flex items-center justify-between px-5 pt-4 pb-2 shrink-0 bg-white">
              <h2 className="text-lg font-semibold text-gray-900">Dashboard</h2>
              <button
                onClick={() => setDashboardOpen(false)}
                className="p-1 rounded hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition"
                aria-label="Collapse dashboard"
              >
                <ChevronDoubleLeftIcon className="w-4 h-4" />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto px-5 pb-4 bg-gray-50/60 custom-scrollbar">
              <ErrorBoundary>
                <Suspense fallback={<PanelFallback />}>
                  <DashboardPanel onChatPrefill={handleChatPrefill} />
                </Suspense>
              </ErrorBoundary>
            </div>
          </div>
        )}

        {/* Chat panel — always visible */}
        <div className="flex-1 flex flex-col overflow-hidden min-w-0 bg-gray-50/50">
          {/* Chat header */}
          <div className={`flex items-center gap-2 px-4 py-2 border-b border-gray-200 bg-white shrink-0 ${chatHighlight ? "chat-highlight" : ""}`}>
            {!dashboardOpen && (
              <button
                onClick={() => setDashboardOpen(true)}
                className="p-1 rounded hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition"
                aria-label="Show dashboard"
              >
                <ChevronDoubleRightIcon className="w-4 h-4" />
              </button>
            )}
            <span className="font-bold text-base" aria-label="Patchy AI Assistant"><span aria-hidden="true">{"\uD83E\uDD16"}</span> Patchy</span>
          </div>

          {/* Chat content */}
          <div className="flex-1 overflow-hidden">
            <ErrorBoundary>
              <Suspense fallback={<PanelFallback />}>
                <ChatPanel
                  messages={messages}
                  isStreaming={isStreaming}
                  onSend={sendMessage}
                  onStop={stopStreaming}
                  onClear={clearMessages}
                  sessionId={sessionId}
                  prefillText={chatPrefill}
                  onPrefillConsumed={() => setChatPrefill("")}
                  role={role}
                />
              </Suspense>
            </ErrorBoundary>
          </div>
        </div>
      </div>
    </div>
  );
}
