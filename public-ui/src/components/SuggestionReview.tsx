"use client";

import { useState, useEffect, useCallback } from "react";
import { fetchSuggestions, respondToSuggestion } from "@/lib/api";
import type { Suggestion } from "@/lib/api";

interface SuggestionReviewProps {
  workspace: string;
  onClose: () => void;
  onAction: () => void;
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
