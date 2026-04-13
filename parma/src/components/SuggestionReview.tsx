"use client";

import { useState, useEffect, useCallback } from "react";
import { fetchSuggestions, respondToSuggestion } from "@/lib/api";
import type { Suggestion } from "@/lib/api";

interface SuggestionReviewProps {
  workspace: string;
  onClose: () => void;
  onAction: () => void;
}

function SuggestionPreview({
  type,
  payload,
}: {
  type: string;
  payload: Record<string, unknown>;
}) {
  if (type === "add_to_branch") {
    const nodeType = (payload.node_type as string) || "claim";
    const headline = (payload.headline as string) || "";
    const content = (payload.content as string) || "";
    if (!headline && !content) return null;
    return (
      <div className="sug-preview">
        <div className="sug-preview-label">will create</div>
        <div className="sug-preview-node">
          <span
            className="sug-preview-type"
            style={{ color: `var(--node-${nodeType}, var(--fg-dim))` }}
          >
            {nodeType}
          </span>
          {headline && <span className="sug-preview-headline">{headline}</span>}
          {content && (
            <div className="sug-preview-content">
              {content.length > 200 ? content.slice(0, 200) + "..." : content}
            </div>
          )}
        </div>
      </div>
    );
  }

  if (type === "relevel_node") {
    const newImp = payload.new_importance;
    if (newImp == null) return null;
    return (
      <div className="sug-preview">
        <div className="sug-preview-label">importance change</div>
        <div className="sug-preview-level">
          <span className="sug-preview-arrow">→ L{String(newImp)}</span>
        </div>
      </div>
    );
  }

  if (type === "resolve_tension") {
    const otherId = (payload.other_node_id as string) || "";
    if (!otherId) return null;
    return (
      <div className="sug-preview">
        <div className="sug-preview-label">will link as opposes</div>
        <div className="sug-preview-tension">
          <span className="sug-preview-id">{otherId.slice(0, 8)}</span>
          <span className="sug-preview-arrow">↔</span>
          <span>target</span>
        </div>
      </div>
    );
  }

  if (type === "merge_duplicate") {
    const keepId = (payload.keep_node_id as string) || "";
    const supersedeId = (payload.supersede_node_id as string) || "";
    if (!keepId && !supersedeId) return null;
    return (
      <div className="sug-preview">
        <div className="sug-preview-label">will merge</div>
        <div className="sug-preview-merge">
          {supersedeId && (
            <span>
              <span className="sug-preview-id">{supersedeId.slice(0, 8)}</span>
              {" → superseded"}
            </span>
          )}
          {keepId && (
            <span>
              <span className="sug-preview-id">{keepId.slice(0, 8)}</span>
              {" kept"}
            </span>
          )}
        </div>
      </div>
    );
  }

  if (type === "cascade_review") {
    const changedHl = (payload.changed_headline as string) || "?";
    const changedId = ((payload.changed_node_id as string) || "").slice(0, 8);
    const depHl = (payload.dependent_headline as string) || "?";
    const changes = (payload.changes as Record<string, { old: unknown; new: unknown }>) || {};
    const changeStrs = Object.entries(changes).map(([field, vals]) =>
      field === "superseded" ? "judgement superseded" : `${field}: ${String(vals.old)}→${String(vals.new)}`
    );
    return (
      <div className="sug-preview">
        <div className="sug-preview-label">cascade: dependency changed</div>
        <div style={{ fontSize: "12px", lineHeight: "1.5", color: "var(--fg-dim)" }}>
          <div>
            <strong>{depHl}</strong> depends on <strong>{changedHl}</strong>{" "}
            <span style={{ opacity: 0.6 }}>[{changedId}]</span>
          </div>
          {changeStrs.length > 0 && (
            <div style={{ marginTop: "4px", fontFamily: "var(--font-mono-stack)", fontSize: "11px" }}>
              {changeStrs.join(", ")}
            </div>
          )}
        </div>
      </div>
    );
  }

  return null;
}

function SuggestionCard({
  suggestion,
  onRespond,
}: {
  suggestion: Suggestion;
  onRespond: (id: string, action: "accept" | "reject") => void;
}) {
  let payload: Record<string, unknown> = {};
  try {
    payload = JSON.parse(suggestion.payload);
  } catch {
    /* empty */
  }
  const reasoning = (payload.reasoning as string) || "";
  const isPending = suggestion.status === "pending";

  return (
    <div className="suggestion-card">
      <div className="suggestion-header">
        <span className="suggestion-type">{suggestion.suggestion_type}</span>
        {suggestion.target_headline && (
          <span className="suggestion-target">
            on {suggestion.target_headline}
          </span>
        )}
      </div>
      {reasoning && (
        <div className="suggestion-reasoning">{reasoning}</div>
      )}
      <SuggestionPreview type={suggestion.suggestion_type} payload={payload} />
      {isPending && (
        <div className="suggestion-actions">
          <button
            className="suggestion-btn suggestion-accept"
            onClick={() => onRespond(suggestion.id, "accept")}
          >
            accept
          </button>
          <button
            className="suggestion-btn suggestion-reject"
            onClick={() => onRespond(suggestion.id, "reject")}
          >
            reject
          </button>
        </div>
      )}
    </div>
  );
}

export function SuggestionReview({
  workspace,
  onClose,
  onAction,
}: SuggestionReviewProps) {
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<"pending" | "accepted" | "rejected">("pending");

  const load = useCallback(() => {
    setLoading(true);
    fetchSuggestions(workspace, tab)
      .then(setSuggestions)
      .catch(() => setSuggestions([]))
      .finally(() => setLoading(false));
  }, [workspace, tab]);

  useEffect(() => {
    load();
  }, [load]);

  const handleRespond = useCallback(
    async (id: string, action: "accept" | "reject") => {
      await respondToSuggestion(id, action);
      onAction();
      load();
    },
    [onAction, load],
  );

  return (
    <div style={{ padding: "32px 36px" }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "baseline",
          marginBottom: "24px",
        }}
      >
        <h2
          style={{
            fontSize: "20px",
            fontWeight: 400,
            margin: 0,
            fontFamily: "var(--font-body-stack)",
          }}
        >
          Review Queue
        </h2>
        <button
          onClick={onClose}
          style={{
            background: "none",
            border: "none",
            cursor: "pointer",
            fontFamily: "var(--font-mono-stack)",
            fontSize: "11px",
            color: "var(--fg-dim)",
            padding: "4px 8px",
          }}
        >
          back to worldview
        </button>
      </div>

      <div className="review-tabs">
        {(["pending", "accepted", "rejected"] as const).map((t) => (
          <button
            key={t}
            className={`review-tab ${tab === t ? "review-tab-active" : ""}`}
            onClick={() => setTab(t)}
          >
            {t}
          </button>
        ))}
      </div>

      {loading ? (
        <div style={{ padding: "20px 0", color: "var(--fg-dim)", fontFamily: "var(--font-mono-stack)", fontSize: "12px" }}>
          Loading...
        </div>
      ) : suggestions.length === 0 ? (
        <div style={{ padding: "20px 0", color: "var(--fg-muted)", fontSize: "14px" }}>
          No {tab} suggestions.
        </div>
      ) : (
        <div>
          {suggestions.map((s) => (
            <SuggestionCard
              key={s.id}
              suggestion={s}
              onRespond={handleRespond}
            />
          ))}
        </div>
      )}
    </div>
  );
}
