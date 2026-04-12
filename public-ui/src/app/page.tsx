"use client";

import { Suspense, useState, useCallback, useEffect } from "react";
import { StackedPanes } from "@/components/StackedPanes";
import { ChatPanel } from "@/components/ChatPanel";
import { fetchWorldview } from "@/lib/api";
import type { Worldview } from "@/lib/types";

function WorldviewContent() {
  const [chatOpen, setChatOpen] = useState(false);
  const toggleChat = useCallback(() => setChatOpen((v) => !v), []);
  const [worldview, setWorldview] = useState<Worldview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [focusNodeId, setFocusNodeId] = useState<string | null>(null);

  const refreshWorldview = useCallback(() => {
    setRefreshKey((k) => k + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetchWorldview("default")
      .then((wv) => {
        if (!cancelled) setWorldview(wv);
      })
      .catch((e) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  if (error) {
    return (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          height: "100vh",
          fontFamily: "var(--font-mono-stack)",
          fontSize: "12px",
          color: "var(--fg-muted)",
          padding: "20px",
          textAlign: "center",
        }}
      >
        Could not load worldview: {error}
        <br />
        Is the API running? (uv run public-ui/serve.py)
      </div>
    );
  }

  if (!worldview) {
    return (
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
    );
  }

  return (
    <div className="layout-with-chat">
      <StackedPanes
        worldview={worldview}
        focusNodeId={focusNodeId}
        onFocusHandled={() => setFocusNodeId(null)}
      />
      <ChatPanel
        questionHeadline={worldview.question_headline}
        isOpen={chatOpen}
        onToggle={toggleChat}
        onMessageSent={refreshWorldview}
        onNodeRef={setFocusNodeId}
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
          Loading...
        </div>
      }
    >
      <WorldviewContent />
    </Suspense>
  );
}
