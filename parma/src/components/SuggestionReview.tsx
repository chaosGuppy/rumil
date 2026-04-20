"use client";

import { useState, useEffect, useCallback } from "react";
import { fetchSuggestions, respondToSuggestion } from "@/lib/api";
import type { Suggestion } from "@/lib/api";

interface SuggestionReviewProps {
  projectId: string;
  onClose: () => void;
  onAction: () => void;
}

const EFFECT_LABEL = "if accepted";

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
        <div className="sug-preview-label">{EFFECT_LABEL}: create a new {nodeType}</div>
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

  if (type === "relevel") {
    const newImp = payload.new_importance;
    if (newImp == null) return null;
    return (
      <div className="sug-preview">
        <div className="sug-preview-label">{EFFECT_LABEL}</div>
        <div className="sug-preview-effect">
          set importance of this page to <strong>L{String(newImp)}</strong>
        </div>
      </div>
    );
  }

  if (type === "resolve_tension") {
    const otherId = (payload.other_node_id as string) || "";
    if (!otherId) return null;
    return (
      <div className="sug-preview">
        <div className="sug-preview-label">{EFFECT_LABEL}</div>
        <div className="sug-preview-effect">
          link this page to <span className="sug-preview-id">{otherId.slice(0, 8)}</span> as{" "}
          <strong>related</strong>
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
        <div className="sug-preview-label">{EFFECT_LABEL}</div>
        <div className="sug-preview-effect">
          {supersedeId && keepId ? (
            <>
              supersede <span className="sug-preview-id">{supersedeId.slice(0, 8)}</span> with{" "}
              <span className="sug-preview-id">{keepId.slice(0, 8)}</span>
            </>
          ) : supersedeId ? (
            <>mark <span className="sug-preview-id">{supersedeId.slice(0, 8)}</span> as superseded</>
          ) : (
            <>keep <span className="sug-preview-id">{keepId.slice(0, 8)}</span></>
          )}
        </div>
        <div className="sug-preview-note">
          reads of the superseded page will follow the pointer to the kept one
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
        <div className="sug-preview-label">{EFFECT_LABEL}: mark as reviewed (no state change)</div>
        <div className="sug-preview-cascade-body">
          <div>
            <strong>{depHl}</strong> depends on <strong>{changedHl}</strong>{" "}
            <span className="sug-preview-dim">[{changedId}]</span>
          </div>
          {changeStrs.length > 0 && (
            <div className="sug-preview-changes">{changeStrs.join(", ")}</div>
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
  const payload = suggestion.payload;
  const reasoning = (payload.reasoning as string) || "";
  const isPending = suggestion.status === "pending";
  const targetId = suggestion.target_page_id?.slice(0, 8) ?? "";

  return (
    <div className="suggestion-card">
      <div className="suggestion-header">
        <span className="suggestion-type">{suggestion.suggestion_type}</span>
        {(suggestion.target_headline || targetId) && (
          <span className="suggestion-target">
            on {suggestion.target_headline ?? targetId}
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

// Suggestion types whose "accept" runs no workspace mutation — they're
// acknowledgement-only. We bubble these to the bottom of the pending list so
// reviewers see state-changing suggestions first.
const NO_OP_TYPES = new Set(["cascade_review", "auto_investigate"]);

function isNoOp(s: Suggestion): boolean {
  return NO_OP_TYPES.has(s.suggestion_type);
}

function GroupedSuggestions({
  suggestions,
  tab,
  onRespond,
}: {
  suggestions: Suggestion[];
  tab: "pending" | "accepted" | "rejected";
  onRespond: (id: string, action: "accept" | "reject") => void;
}) {
  // Only split in the pending tab — accepted/rejected history reads best
  // chronologically, without a state-change divider.
  if (tab !== "pending") {
    return (
      <div>
        {suggestions.map((s) => (
          <SuggestionCard key={s.id} suggestion={s} onRespond={onRespond} />
        ))}
      </div>
    );
  }
  const mutations = suggestions.filter((s) => !isNoOp(s));
  const noOps = suggestions.filter(isNoOp);
  return (
    <div>
      {mutations.length > 0 && (
        <section className="review-group">
          <h3 className="review-group-title">
            state-changing <span className="review-group-count">{mutations.length}</span>
          </h3>
          {mutations.map((s) => (
            <SuggestionCard key={s.id} suggestion={s} onRespond={onRespond} />
          ))}
        </section>
      )}
      {noOps.length > 0 && (
        <section className="review-group review-group-noop">
          <h3 className="review-group-title">
            review only · no state change{" "}
            <span className="review-group-count">{noOps.length}</span>
          </h3>
          {noOps.map((s) => (
            <SuggestionCard key={s.id} suggestion={s} onRespond={onRespond} />
          ))}
        </section>
      )}
    </div>
  );
}

export function SuggestionReview({
  projectId,
  onClose,
  onAction,
}: SuggestionReviewProps) {
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<"pending" | "accepted" | "rejected">("pending");

  const load = useCallback(() => {
    setLoading(true);
    fetchSuggestions(projectId, tab)
      .then(setSuggestions)
      .catch(() => setSuggestions([]))
      .finally(() => setLoading(false));
  }, [projectId, tab]);

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
    <div className="pane-review-inner">
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
          back to view
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
        <GroupedSuggestions
          suggestions={suggestions}
          tab={tab}
          onRespond={handleRespond}
        />
      )}
    </div>
  );
}
