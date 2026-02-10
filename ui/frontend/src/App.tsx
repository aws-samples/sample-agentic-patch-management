import { useState } from "react";
import AppLayout from "@cloudscape-design/components/app-layout";
import ContentLayout from "@cloudscape-design/components/content-layout";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import SplitPanel from "@cloudscape-design/components/split-panel";
import TopNavigation from "@cloudscape-design/components/top-navigation";
import ChatPanel from "./components/ChatPanel";
import { useAgentStream } from "./hooks/useAgentStream";

export default function App() {
  const { messages, isStreaming, sessionId, sendMessage, stopStreaming, clearMessages } =
    useAgentStream();
  const [splitOpen, setSplitOpen] = useState(true);

  return (
    <>
      <TopNavigation
        identity={{
          href: "/",
          title: "Intelligent Patch Automation",
          logo: { src: "", alt: "🛡️" },
        }}
        utilities={[
          {
            type: "button",
            text: "Chat",
            iconName: "contact",
            onClick: () => setSplitOpen((prev) => !prev),
          },
        ]}
      />
      <AppLayout
        contentType="default"
        navigationHide
        toolsHide
        splitPanel={
          <SplitPanel
            header="Agent Chat"
            hidePreferencesButton
            closeBehavior="hide"
          >
            <div style={{ height: "calc(100vh - 220px)" }}>
              <ChatPanel
                messages={messages}
                isStreaming={isStreaming}
                onSend={sendMessage}
                onStop={stopStreaming}
                onClear={clearMessages}
                sessionId={sessionId}
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
            <Box padding="l" textAlign="center" color="text-body-secondary">
              <Box variant="h2">Dashboard panels coming in Phase 2</Box>
              <Box variant="p" margin={{ top: "s" }}>
                Environment status, vulnerability findings, patching activity, and compliance
                summary will appear here. For now, use the chat panel on the right to interact
                with Patchy.
              </Box>
            </Box>
          </ContentLayout>
        }
      />
    </>
  );
}
