import { useState, useCallback } from "react";
import TopNavigation from "@cloudscape-design/components/top-navigation";
import Header from "@cloudscape-design/components/header";
import Button from "@cloudscape-design/components/button";
import ChatPanel from "./components/ChatPanel";
import DashboardPanel from "./components/DashboardPanel";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { useAgentStream } from "./hooks/useAgentStream";
import { useRole } from "./hooks/useRole";
import type { Role } from "./hooks/useRole";

export default function App() {
  const { role, setRole } = useRole();
  const { messages, isStreaming, sessionId, sendMessage, stopStreaming, clearMessages } =
    useAgentStream(role);
  const [chatPrefill, setChatPrefill] = useState("");
  const [dashboardOpen, setDashboardOpen] = useState(true);

  const handleChatPrefill = useCallback((text: string) => {
    setChatPrefill(text);
  }, []);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh", overflow: "hidden" }}>
      <TopNavigation
        identity={{ href: "/", title: "🛡️ Patchy — Intelligent Patch Automation" }}
        utilities={[
          {
            type: "menu-dropdown",
            text: role === "operator" ? "👤 Operator" : "👁️ Viewer",
            title: "Switch role",
            items: [
              { id: "operator", text: "👤 Operator — full access" },
              { id: "viewer", text: "👁️ Viewer — read-only" },
            ],
            onItemClick: ({ detail }) => setRole(detail.id as Role),
          },
        ]}
      />

      <div style={{ display: "flex", flex: 1, overflow: "hidden" }}>
        {/* Dashboard panel — collapsible with transition */}
        <div style={{
          width: dashboardOpen ? "72%" : "0%",
          minWidth: dashboardOpen ? 500 : 0,
          overflow: "hidden",
          borderRight: dashboardOpen ? "1px solid #e9ebed" : "none",
          transition: "width 0.3s ease, min-width 0.3s ease",
        }}>
          <div style={{ padding: "16px 20px", minWidth: 500, height: "100%", overflowY: "auto" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
              <Header variant="h2">Dashboard</Header>
              <Button
                iconName="angle-left-double"
                variant="icon"
                onClick={() => setDashboardOpen(false)}
                ariaLabel="Collapse dashboard"
              />
            </div>
            <ErrorBoundary>
              <DashboardPanel onChatPrefill={handleChatPrefill} />
            </ErrorBoundary>
          </div>
        </div>

        {/* Chat panel — always visible, expands when dashboard collapsed */}
        <div style={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
          minWidth: 0,
          background: "#fafafa",
          transition: "flex 0.3s ease",
        }}>
          {/* Chat header */}
          <div style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "8px 16px",
            borderBottom: "1px solid #e9ebed",
            background: "#fff",
            flexShrink: 0,
          }}>
            {!dashboardOpen && (
              <Button
                iconName="angle-right-double"
                variant="icon"
                onClick={() => setDashboardOpen(true)}
                ariaLabel="Show dashboard"
              />
            )}
            <span style={{ fontWeight: 700, fontSize: 16 }}>🤖 Patchy</span>
          </div>

          {/* Chat content */}
          <div style={{ flex: 1, overflow: "hidden" }}>
            <ErrorBoundary>
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
            </ErrorBoundary>
          </div>
        </div>
      </div>
    </div>
  );
}
