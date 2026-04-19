"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import type { CallsForQuestion, Subgraph } from "@/api";

export type FocusColorMap = Record<string, string>;

type FocusBarSegment = {
  callType: string;
  n: number;
  color: string;
};

type FocusBar =
  | {
      kind: "calls";
      label: string;
      total: number;
      segments: FocusBarSegment[];
    }
  | {
      kind: "count";
      key: "child_questions" | "considerations" | "judgements";
      label: string;
      total: number;
      color: string;
      hatchColor?: string;
    };

function buildFocusBars(
  entry: CallsForQuestion,
  callTypes: string[],
  hiddenCallTypes: Set<string>,
  callTypeColor: (ct: string, i: number) => string,
): FocusBar[] {
  const visibleTypes = callTypes.filter((ct) => !hiddenCallTypes.has(ct));
  const segments: FocusBarSegment[] = [];
  let callsTotal = 0;
  for (const ct of visibleTypes) {
    const n = entry.by_type[ct] ?? 0;
    if (n > 0) {
      segments.push({
        callType: ct,
        n,
        color: callTypeColor(ct, callTypes.indexOf(ct)),
      });
      callsTotal += n;
    }
  }
  return [
    {
      kind: "calls",
      label: "calls",
      total: callsTotal,
      segments,
    },
    {
      kind: "count",
      key: "child_questions",
      label: "child questions",
      total: entry.child_questions,
      color: "var(--type-question)",
      hatchColor: "var(--type-question-border)",
    },
    {
      kind: "count",
      key: "considerations",
      label: "considerations",
      total: entry.considerations,
      color: "var(--type-claim)",
      hatchColor: "var(--type-claim-border)",
    },
    {
      kind: "count",
      key: "judgements",
      label: "judgements",
      total: entry.judgements,
      color: "var(--type-judgement)",
      hatchColor: "var(--type-judgement-border)",
    },
  ];
}

function maxFromBars(bars: FocusBar[]): number {
  let max = 0;
  for (const b of bars) if (b.total > max) max = b.total;
  return max;
}

// Shared renderer for a single focus bar row.
function FocusBarRow({
  bar,
  max,
  compact = false,
  animationDelayMs = 0,
}: {
  bar: FocusBar;
  max: number;
  compact?: boolean;
  animationDelayMs?: number;
}) {
  const pct = max === 0 ? 0 : (bar.total / max) * 100;
  return (
    <div className={`focus-row${compact ? " compact" : ""}`}>
      <span
        className="focus-label"
        style={{
          color: bar.kind === "count" ? bar.color : "var(--color-foreground)",
        }}
      >
        {bar.label}
      </span>
      <div className="focus-track">
        <div
          className="focus-fill"
          style={{
            width: `${pct}%`,
            animationDelay: `${animationDelayMs}ms`,
          }}
        >
          {bar.kind === "calls" ? (
            <CallsStack segments={bar.segments} total={bar.total} />
          ) : (
            <div
              className="count-fill"
              style={{
                background: bar.hatchColor ?? bar.color,
                borderRight: `2px solid ${bar.color}`,
              }}
            />
          )}
        </div>
      </div>
      <span className="focus-count">{bar.total}</span>
    </div>
  );
}

function CallsStack({
  segments,
  total,
}: {
  segments: FocusBarSegment[];
  total: number;
}) {
  if (total === 0 || segments.length === 0) {
    return <div className="count-fill empty" />;
  }
  return (
    <div className="calls-stack">
      {segments.map((s) => {
        const pct = (s.n / total) * 100;
        return (
          <div
            key={s.callType}
            className="calls-seg"
            style={{ width: `${pct}%`, background: s.color }}
            title={`${s.callType} · ${s.n}`}
          />
        );
      })}
    </div>
  );
}

export function QuestionFocusBars({
  entry,
  callTypes,
  hiddenCallTypes,
  callTypeColor,
  compact = false,
}: {
  entry: CallsForQuestion;
  callTypes: string[];
  hiddenCallTypes: Set<string>;
  callTypeColor: (ct: string, i: number) => string;
  compact?: boolean;
}) {
  const bars = useMemo(
    () => buildFocusBars(entry, callTypes, hiddenCallTypes, callTypeColor),
    [entry, callTypes, hiddenCallTypes, callTypeColor],
  );
  const max = maxFromBars(bars);

  return (
    <div className={`focus-bars${compact ? " compact" : ""}`}>
      <FocusBarStyles />
      {bars.map((bar, i) => (
        <FocusBarRow
          key={bar.kind === "calls" ? "calls" : bar.key}
          bar={bar}
          max={max}
          compact={compact}
          animationDelayMs={i * 60}
        />
      ))}
    </div>
  );
}

function FocusBarStyles() {
  return (
    <style>{`
      .focus-bars {
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
      }
      .focus-bars.compact {
        gap: 0.3rem;
      }
      .focus-row {
        display: grid;
        grid-template-columns: 7.5rem 1fr 3rem;
        align-items: center;
        gap: 0.75rem;
        font-family: var(--font-geist-mono), monospace;
        font-size: 0.75rem;
      }
      .focus-row.compact {
        grid-template-columns: 5.5rem 1fr 2rem;
        gap: 0.5rem;
        font-size: 0.68rem;
      }
      .focus-label {
        text-align: right;
        letter-spacing: 0.02em;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .focus-track {
        position: relative;
        height: 0.95rem;
        background: var(--color-background);
        border: 1px solid var(--color-border);
      }
      .focus-row.compact .focus-track {
        height: 0.72rem;
      }
      .focus-fill {
        position: absolute;
        top: 0;
        left: 0;
        bottom: 0;
        transform-origin: left;
        animation: focusBarGrow 0.55s cubic-bezier(0.2, 0.8, 0.2, 1) both;
      }
      @keyframes focusBarGrow {
        from { transform: scaleX(0); }
        to { transform: scaleX(1); }
      }
      .count-fill {
        width: 100%;
        height: 100%;
      }
      .count-fill.empty {
        background: transparent;
      }
      .calls-stack {
        display: flex;
        width: 100%;
        height: 100%;
      }
      .calls-seg {
        height: 100%;
      }
      .calls-seg + .calls-seg {
        border-left: 1px solid var(--color-background);
      }
      .focus-count {
        font-variant-numeric: tabular-nums;
        text-align: right;
        opacity: 0.85;
      }
    `}</style>
  );
}

// Extract the set of direct child questions of the anchor from the subgraph.
// We walk edges for (anchor --child_question--> dst).
function subquestionIdsFromSubgraph(
  subgraph: Subgraph,
  anchorId: string,
): string[] {
  const ids: string[] = [];
  const seen = new Set<string>();
  for (const e of subgraph.edges) {
    if (
      e.link_type === "child_question" &&
      e.from_page_id === anchorId &&
      !seen.has(e.to_page_id)
    ) {
      seen.add(e.to_page_id);
      ids.push(e.to_page_id);
    }
  }
  return ids;
}

export function SubquestionFocusGrid({
  anchorId,
  subgraph,
  callsByQuestion,
  callTypes,
  hiddenCallTypes,
  callTypeColor,
}: {
  anchorId: string;
  subgraph: Subgraph;
  callsByQuestion: Record<string, CallsForQuestion>;
  callTypes: string[];
  hiddenCallTypes: Set<string>;
  callTypeColor: (ct: string, i: number) => string;
}) {
  const subquestionIds = useMemo(
    () => subquestionIdsFromSubgraph(subgraph, anchorId),
    [subgraph, anchorId],
  );
  const [limit, setLimit] = useState(6);

  if (subquestionIds.length === 0) {
    return (
      <div className="empty-panel">
        this question has no child questions
      </div>
    );
  }

  const visibleIds = subquestionIds.slice(0, limit);
  const hiddenCount = subquestionIds.length - visibleIds.length;

  return (
    <div className="subq-grid-wrap">
      <style>{`
        .subq-grid {
          display: grid;
          grid-template-columns: repeat(auto-fill, minmax(20rem, 1fr));
          gap: 1px;
          background: var(--color-border);
          border: 1px solid var(--color-border);
        }
        .subq-cell {
          background: var(--color-background);
          padding: 0.9rem 1rem 1rem 1rem;
          display: flex;
          flex-direction: column;
          gap: 0.6rem;
          animation: subqFadeIn 0.35s ease both;
        }
        @keyframes subqFadeIn {
          from { opacity: 0; transform: translateY(3px); }
          to { opacity: 1; transform: translateY(0); }
        }
        .subq-header {
          display: flex;
          flex-direction: column;
          gap: 0.2rem;
          min-height: 2.4rem;
        }
        .subq-index {
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.62rem;
          letter-spacing: 0.12em;
          text-transform: uppercase;
          color: var(--type-question);
          opacity: 0.75;
          display: flex;
          justify-content: space-between;
          align-items: baseline;
          gap: 0.5rem;
        }
        .subq-index .total-pill {
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.62rem;
          letter-spacing: 0.04em;
          text-transform: none;
          color: var(--color-muted);
          opacity: 0.85;
        }
        .subq-headline {
          font-size: 0.82rem;
          line-height: 1.3;
          color: var(--color-foreground);
          font-weight: 500;
          display: -webkit-box;
          -webkit-line-clamp: 2;
          -webkit-box-orient: vertical;
          overflow: hidden;
        }
        .subq-headline a {
          color: inherit;
          text-decoration: none;
          border-bottom: 1px solid transparent;
          transition: border-color 0.12s ease;
        }
        .subq-headline a:hover {
          border-color: var(--type-question);
        }
        .subq-missing {
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.7rem;
          color: var(--color-muted);
          letter-spacing: 0.03em;
          padding: 0.5rem 0;
        }
        .subq-more {
          margin-top: 0.75rem;
          padding: 0.5rem 0.75rem;
          background: transparent;
          border: 1px solid var(--color-border);
          color: var(--color-muted);
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.68rem;
          letter-spacing: 0.08em;
          text-transform: uppercase;
          cursor: pointer;
          transition: all 0.12s ease;
        }
        .subq-more:hover {
          color: var(--color-foreground);
          border-color: var(--color-accent);
        }
      `}</style>

      <div className="subq-grid">
        {visibleIds.map((qid, idx) => {
          const entry = callsByQuestion[qid];
          const headline =
            entry?.headline ?? inferHeadline(subgraph, qid) ?? "(no headline)";
          return (
            <div key={qid} className="subq-cell">
              <div className="subq-header">
                <div className="subq-index">
                  <span>SQ{String(idx + 1).padStart(2, "0")}</span>
                  {entry ? (
                    <span className="total-pill">
                      {entry.total} call{entry.total === 1 ? "" : "s"}
                    </span>
                  ) : null}
                </div>
                <div className="subq-headline">
                  <Link href={`/pages/${qid}/stats`}>{headline}</Link>
                </div>
              </div>
              {entry ? (
                <QuestionFocusBars
                  entry={entry}
                  callTypes={callTypes}
                  hiddenCallTypes={hiddenCallTypes}
                  callTypeColor={callTypeColor}
                  compact
                />
              ) : (
                <div className="subq-missing">
                  no stats available for this subquestion
                </div>
              )}
            </div>
          );
        })}
      </div>

      {hiddenCount > 0 && (
        <button
          type="button"
          className="subq-more"
          onClick={() => setLimit((n) => n + 6)}
        >
          show {Math.min(hiddenCount, 6)} more · {hiddenCount} hidden
        </button>
      )}
    </div>
  );
}

function inferHeadline(subgraph: Subgraph, qid: string): string | null {
  const node = subgraph.nodes.find((n) => n.id === qid);
  return node?.headline ?? null;
}
