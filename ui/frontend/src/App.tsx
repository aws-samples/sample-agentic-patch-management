import { useState, useCallback } from "react";
import AppLayout from "@cloudscape-design/components/app-layout";
import ContentLayout from "@cloudscape-design/components/content-layout";
import Header from "@cloudscape-design/components/header";
import SplitPanel from "@cloudscape-design/components/split-panel";
import TopNavigation from "@cloudscape-design/components/top-navigation";
import ChatPanel from "./components/ChatPanel";
import DashboardPanel from "./components/DashboardPanel";
import { useAgentStream } from "./hooks/useAgentStream";

export default function App() {
  const { messages, isStreaming, sessionId, sendMessage, stopStreaming, clearMessages } =
    useAgentStream();
  const [splitOpen, setSplitOpen] = useState(true);
  const [chatPrefill, setChatPrefill] = useState("");

  const handleChatPrefill = useCallback((text: string) => {
    setChatPrefill(text);
    setSplitOpen(true);
  }, []);

  return (
    <>
      <TopNavigation
        identity={{
          href: "/",
          title: "Intelligent Patch Automation",
        }}
        utilities={[]}
      />
      <AppLayout
        contentType="default"
        navigationHide
        toolsHide
        splitPanel={
          <SplitPanel
            header="🤖 Patchy — Agent Chat"
            hidePreferencesButton
            closeBehavior="hide"
          >
            <div>
              <ChatPanel
                messages={messages}
                isStreaming={isStreaming}
                onSend={sendMessage}
                onStop={stopStreaming}
                onClear={clearMessages}
                sessionId={sessionId}
                prefillText={chatPrefill}
                onPrefillConsumed={() => setChatPrefill("")}
              />
            </div>
          </SplitPanel>
        }
        splitPanelOpen={splitOpen}
        onSplitPanelToggle={({ detail }) => setSplitOpen(detail.open)}
        splitPanelPreferences={{ position: "side" }}
        content={
          <ContentLayout
            header={
              <Header
                variant="h1"
                description="Vulnerability-driven patch management powered by AI agents"
              >
                🛡️ Patch Automation Dashboard
              </Header>
            }
          >
            <DashboardPanel onChatPrefill={handleChatPrefill} />
          </ContentLayout>
        }
      />
    </>
  );
}
