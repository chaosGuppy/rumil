"use client";

import { useState, useCallback, useEffect } from "react";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { StackedPanes } from "@/components/StackedPanes";
import { ChatPanel } from "@/components/ChatPanel";
import { SuggestionReview } from "@/components/SuggestionReview";
import { fetchWorldview, fetchWorkspaces } from "@/lib/api";
import type { Worldview } from "@/lib/types";
import type { WorkspaceInfo } from "@/lib/api";

function WorkspaceBrowser({
  onSelect,
}: {
  onSelect: (name: string) => void;
}) {
  const [workspaces, setWorkspaces] = useState<WorkspaceInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [newName, setNewName] = useState("");
  const [newQuestion, setNewQuestion] = useState("");
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    fetchWorkspaces()
      .then(setWorkspaces)
      .catch(() => setWorkspaces([]))
      .finally(() => setLoading(false));
  }, []);

  const handleCreate = useCallback(async () => {
    if (!newQuestion.trim()) return;
    setCreating(true);
    const name = newName.trim() || newQuestion.trim().slice(0, 40).toLowerCase().replace(/[^a-z0-9]+/g, "-");
    try {
      const { createWorkspace } = await import("@/lib/api");
      await createWorkspace(name, newQuestion.trim());
      setNewName("");
      setNewQuestion("");
      const updated = await fetchWorkspaces();
      setWorkspaces(updated);
      onSelect(name);
    } finally {
      setCreating(false);
    }
  }, [newName, newQuestion, onSelect]);

  if (loading) {
    return (
      <div className="browser-loading">Loading workspaces...</div>
    );
  }

  return (
    <div className="browser">
      <div className="browser-header">
        <h1 className="browser-title">Worldview</h1>
        <p className="browser-subtitle">
          Research workspaces. Pick one to explore, or start a new investigation.
        </p>
      </div>

      {workspaces.length > 0 && (
        <div className="browser-list">
          {workspaces.map((ws) => (
            <button
              key={ws.id}
              className="browser-card"
              onClick={() => onSelect(ws.name)}
            >
              <div className="browser-card-name">{ws.name}</div>
              <div className="browser-card-stats">
                {ws.node_count} nodes
                {ws.run_count > 0 && ` · ${ws.run_count} runs`}
                {ws.pending_suggestions > 0 && (
                  <span className="browser-card-badge">
                    {ws.pending_suggestions} pending
                  </span>
                )}
              </div>
            </button>
          ))}
        </div>
      )}

      <div className="browser-create">
        <div className="browser-create-label">New investigation</div>
        <input
          type="text"
          value={newQuestion}
          onChange={(e) => setNewQuestion(e.target.value)}
          placeholder="What question do you want to investigate?"
          className="browser-input browser-input-main"
          onKeyDown={(e) => e.key === "Enter" && handleCreate()}
        />
        <div className="browser-create-row">
          <input
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="Workspace name (auto-generated if blank)"
            className="browser-input"
          />
          <button
            className="browser-create-btn"
            onClick={handleCreate}
            disabled={!newQuestion.trim() || creating}
          >
            {creating ? "..." : "Start"}
          </button>
        </div>
      </div>
    </div>
  );
}

function WorldviewView({ workspace }: { workspace: string }) {
  const [chatOpen, setChatOpen] = useState(false);
  const toggleChat = useCallback(() => setChatOpen((v) => !v), []);
  const [worldview, setWorldview] = useState<Worldview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [focusNodeId, setFocusNodeId] = useState<string | null>(null);
  const [showReview, setShowReview] = useState(false);

  const refreshWorldview = useCallback(() => {
    setRefreshKey((k) => k + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetchWorldview(workspace)
      .then((wv) => {
        if (!cancelled) setWorldview(wv);
      })
      .catch((e) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey, workspace]);

  if (error) {
    return (
      <div className="view-error">
        Could not load worldview: {error}
        <br />
        Is the API running? (uv run public-ui/serve.py)
      </div>
    );
  }

  if (!worldview) {
    return <div className="view-loading">Loading worldview...</div>;
  }

  return (
    <div className="layout-with-chat">
      {showReview ? (
        <div className="pane-container">
          <div className="pane" style={{ minWidth: "500px" }}>
            <SuggestionReview
              workspace={workspace}
              onClose={() => setShowReview(false)}
              onAction={refreshWorldview}
            />
          </div>
        </div>
      ) : (
        <StackedPanes
          worldview={worldview}
          focusNodeId={focusNodeId}
          onFocusHandled={() => setFocusNodeId(null)}
        />
      )}
      <ChatPanel
        questionHeadline={worldview.question_headline}
        isOpen={chatOpen}
        onToggle={toggleChat}
        onMessageSent={refreshWorldview}
        onNodeRef={setFocusNodeId}
        onShowReview={() => setShowReview(true)}
        workspace={workspace}
      />
    </div>
  );
}

function AppContent() {
  const searchParams = useSearchParams();
  const wsParam = searchParams.get("ws");
  const [selectedWs, setSelectedWs] = useState<string | null>(wsParam);

  const handleSelect = useCallback((name: string) => {
    setSelectedWs(name);
    window.history.replaceState(null, "", `?ws=${encodeURIComponent(name)}`);
  }, []);

  if (!selectedWs) {
    return <WorkspaceBrowser onSelect={handleSelect} />;
  }

  return <WorldviewView workspace={selectedWs} />;
}

export default function Page() {
  return (
    <Suspense
      fallback={<div className="view-loading">Loading...</div>}
    >
      <AppContent />
    </Suspense>
  );
}
