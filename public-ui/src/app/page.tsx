"use client";

import { Suspense, useState, useCallback } from "react";
import { MOCK_WORLDVIEW } from "@/lib/mock-data";
import { StackedPanes } from "@/components/StackedPanes";
import { ChatPanel } from "@/components/ChatPanel";

function WorldviewContent() {
  const [chatOpen, setChatOpen] = useState(false);
  const toggleChat = useCallback(() => setChatOpen((v) => !v), []);

  return (
    <div className="layout-with-chat">
      <StackedPanes worldview={MOCK_WORLDVIEW} />
      <ChatPanel
        questionHeadline={MOCK_WORLDVIEW.question_headline}
        isOpen={chatOpen}
        onToggle={toggleChat}
      />
    </div>
  );
}

export default function Page() {
  return (
    <Suspense
      fallback={
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            height: "100vh",
            fontFamily: "var(--font-mono-stack)",
            fontSize: "12px",
            color: "var(--fg-dim)",
            letterSpacing: "0.08em",
            textTransform: "uppercase",
          }}
        >
          Loading worldview...
        </div>
      }
    >
      <WorldviewContent />
    </Suspense>
  );
}
