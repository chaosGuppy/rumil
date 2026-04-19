"use client";

import { useCallback, useEffect, useState } from "react";
import type {
  CreateNudgeIn,
  NudgeDurability,
  NudgeKind,
  NudgeScope,
} from "@/api/types.gen";
import {
  createNudge,
  fetchNudges,
  revokeNudge,
  type RunNudge,
} from "@/lib/api";

type ComposeKind = "note" | "constrain" | "rewrite" | "pause";

interface ComposeState {
  kind: ComposeKind;
  text: string;
  banTypes: string;
  questions: string;
  persistent: boolean;
}

const EMPTY_COMPOSE: ComposeState = {
  kind: "note",
  text: "",
  banTypes: "",
  questions: "",
  persistent: false,
};

const KIND_LABEL: Record<ComposeKind, string> = {
  note: "note",
  constrain: "constrain",
  rewrite: "rewrite goal",
  pause: "pause",
};

function csv(raw: string): string[] | undefined {
  const parts = raw
    .split(",")
    .map((p) => p.trim())
    .filter(Boolean);
  return parts.length > 0 ? parts : undefined;
}

function buildCreatePayload(c: ComposeState): CreateNudgeIn {
  const scope: NudgeScope = {
    call_types: null,
    question_ids: null,
    call_id: null,
    expires_at: null,
    expires_after_n_calls: null,
  };
  let kind: NudgeKind;
  let hard = false;
  let durability: NudgeDurability = c.persistent ? "persistent" : "one_shot";

  if (c.kind === "note") {
    kind = "inject_note";
    scope.question_ids = csv(c.questions) ?? null;
  } else if (c.kind === "constrain") {
    kind = "constrain_dispatch";
    hard = true;
    scope.call_types = csv(c.banTypes) ?? null;
    scope.question_ids = csv(c.questions) ?? null;
  } else if (c.kind === "rewrite") {
    kind = "rewrite_goal";
    durability = "persistent";
  } else {
    kind = "pause";
    hard = true;
    durability = "persistent";
  }

  return {
    kind,
    durability,
    author_kind: "human",
    author_note: `via parma nudge panel (${c.kind})`,
    payload: {},
    scope,
    soft_text: c.text.trim() || null,
    hard,
  };
}

function formatScope(scope: NudgeScope): string {
  const bits: string[] = [];
  if (scope.call_types && scope.call_types.length > 0)
    bits.push(`types=${scope.call_types.join(",")}`);
  if (scope.question_ids && scope.question_ids.length > 0)
    bits.push(`questions=${scope.question_ids.map((q) => q.slice(0, 8)).join(",")}`);
  if (scope.call_id) bits.push(`call=${scope.call_id.slice(0, 8)}`);
  if (scope.expires_after_n_calls != null)
    bits.push(`expires_in=${scope.expires_after_n_calls}`);
  return bits.join(" · ") || "run-wide";
}

export function NudgePanel({ runId }: { runId: string }) {
  const [nudges, setNudges] = useState<RunNudge[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [compose, setCompose] = useState<ComposeState>(EMPTY_COMPOSE);
  const [submitting, setSubmitting] = useState(false);

  const reload = useCallback(() => {
    fetchNudges(runId, "active")
      .then((ns) => {
        setNudges(ns);
        setLoading(false);
        setError(null);
      })
      .catch((e: Error) => {
        setError(e.message);
        setLoading(false);
      });
  }, [runId]);

  useEffect(() => {
    reload();
  }, [reload]);

  const onSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (submitting) return;
      if (!compose.text.trim() && compose.kind !== "pause") {
        setError("Add a note / reason before firing.");
        return;
      }
      if (compose.kind === "constrain" && !compose.banTypes.trim()) {
        setError("constrain needs at least one call type in ban-types.");
        return;
      }
      setSubmitting(true);
      setError(null);
      try {
        await createNudge(runId, buildCreatePayload(compose));
        setCompose(EMPTY_COMPOSE);
        reload();
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setSubmitting(false);
      }
    },
    [runId, compose, submitting, reload],
  );

  const onRevoke = useCallback(
    async (id: string) => {
      try {
        await revokeNudge(id);
        reload();
      } catch (err) {
        setError((err as Error).message);
      }
    },
    [reload],
  );

  return (
    <section className="op-nudge-panel">
      <div className="op-nudge-panel-header">
        <span className="op-nudge-panel-title">steering</span>
        <span className="op-nudge-panel-count">
          {loading ? "…" : `${nudges.length} active`}
        </span>
      </div>

      {error && <div className="op-nudge-error">{error}</div>}

      <ul className="op-nudge-list">
        {nudges.length === 0 && !loading && (
          <li className="op-nudge-empty">no active nudges</li>
        )}
        {nudges.map((n) => (
          <li key={n.id} className="op-nudge-row">
            <div className="op-nudge-row-top">
              <span className={`op-nudge-kind op-nudge-kind-${n.kind}`}>
                {n.kind}
              </span>
              <span className="op-nudge-flag">
                {n.hard ? "hard" : "soft"} · {n.durability}
              </span>
              <span className="op-nudge-scope">{formatScope(n.scope)}</span>
              <button
                type="button"
                className="op-nudge-revoke"
                onClick={() => onRevoke(n.id)}
              >
                revoke
              </button>
            </div>
            {n.soft_text && (
              <div className="op-nudge-text">{n.soft_text}</div>
            )}
            <div className="op-nudge-meta">
              <span>by {n.author_kind}</span>
              <span>{n.id.slice(0, 8)}</span>
            </div>
          </li>
        ))}
      </ul>

      <form className="op-nudge-compose" onSubmit={onSubmit}>
        <div className="op-nudge-compose-chips">
          {(["note", "constrain", "rewrite", "pause"] as ComposeKind[]).map(
            (k) => (
              <button
                key={k}
                type="button"
                className={`op-nudge-chip ${compose.kind === k ? "is-active" : ""}`}
                onClick={() => setCompose({ ...compose, kind: k })}
              >
                {KIND_LABEL[k]}
              </button>
            ),
          )}
        </div>

        {compose.kind !== "pause" && (
          <textarea
            className="op-nudge-text-input"
            placeholder={
              compose.kind === "note"
                ? "soft note — guidance for upcoming calls"
                : compose.kind === "constrain"
                  ? "optional reason for the ban"
                  : "new framing for the run's goal"
            }
            value={compose.text}
            onChange={(e) => setCompose({ ...compose, text: e.target.value })}
            rows={2}
          />
        )}

        {compose.kind === "constrain" && (
          <input
            className="op-nudge-scope-input"
            placeholder="ban types, comma-separated (e.g. web_research,assess)"
            value={compose.banTypes}
            onChange={(e) =>
              setCompose({ ...compose, banTypes: e.target.value })
            }
          />
        )}

        {(compose.kind === "note" || compose.kind === "constrain") && (
          <input
            className="op-nudge-scope-input"
            placeholder="scope to question ids (optional, comma-separated)"
            value={compose.questions}
            onChange={(e) =>
              setCompose({ ...compose, questions: e.target.value })
            }
          />
        )}

        <div className="op-nudge-compose-row">
          {compose.kind !== "rewrite" && compose.kind !== "pause" && (
            <label className="op-nudge-toggle">
              <input
                type="checkbox"
                checked={compose.persistent}
                onChange={(e) =>
                  setCompose({ ...compose, persistent: e.target.checked })
                }
              />
              persistent
            </label>
          )}
          <div className="op-nudge-compose-actions">
            <button
              type="submit"
              className="op-nudge-submit"
              disabled={submitting}
            >
              {submitting ? "firing…" : `fire ${KIND_LABEL[compose.kind]}`}
            </button>
          </div>
        </div>
      </form>
    </section>
  );
}
