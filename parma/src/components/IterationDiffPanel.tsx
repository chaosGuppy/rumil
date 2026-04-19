"use client";

import { useEffect, useMemo, useState } from "react";
import { diffLines, type Change } from "diff";

import {
  fetchPageIterations,
  type PageIterations,
  type RefineIteration,
  type RefineIterationVerdict,
} from "@/lib/api";

// Surfaces the draft chain of a refine-artifact run as a two-pane line-level
// diff. Opens inside the inspect drawer when the page under inspection is a
// refine-artifact (an ARTIFACT carrying extra.refinement). The panel
// suppresses itself quietly when the backend responds 400 — non-refined
// artifacts are common and we don't want to shove an error into the reader's
// face.

export interface IterationDiffPanelProps {
  pageId: string;
}

export function IterationDiffPanel({ pageId }: IterationDiffPanelProps) {
  const [data, setData] = useState<PageIterations | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setData(null);
    fetchPageIterations(pageId)
      .then((result) => {
        if (cancelled) return;
        setData(result);
        setLoading(false);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e?.message ?? "failed to load iterations");
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [pageId]);

  // A 400 comes back as data=null (fetchPageIterations masks it). Single-
  // iteration runs are trivially uninteresting to diff, so we also hide the
  // panel there — users can still read the draft in the main body above.
  if (loading) {
    return (
      <section className="iter-diff">
        <div className="iter-diff-label">iterations</div>
        <div className="iter-diff-loading">loading…</div>
      </section>
    );
  }
  if (error) {
    return (
      <section className="iter-diff">
        <div className="iter-diff-label">iterations</div>
        <div className="iter-diff-error">{error}</div>
      </section>
    );
  }
  if (!data || data.iterations.length < 2) return null;

  return <IterationDiffInner iterations={data.iterations} />;
}

function IterationDiffInner({
  iterations,
}: {
  iterations: RefineIteration[];
}) {
  // Default to comparing the first and last — that's the "what did all this
  // refinement actually change?" framing. The dropdowns let the user inspect
  // any intermediate pair.
  const [aIdx, setAIdx] = useState(0);
  const [bIdx, setBIdx] = useState(iterations.length - 1);

  const iterA = iterations[aIdx];
  const iterB = iterations[bIdx];

  // Rendering each diff independently per pane (rather than a single merged
  // hunk list) keeps the side-by-side reading flow intact — the reader sees
  // v1 on the left as it was, with removed lines struck through, and v3 on
  // the right with added lines highlighted. diffLines gives one Change[]
  // array with both sides; we split it into the per-pane projections below.
  const changes: Change[] = useMemo(
    () => diffLines(iterA.content, iterB.content),
    [iterA.content, iterB.content],
  );

  return (
    <section className="iter-diff">
      <div className="iter-diff-header">
        <div className="iter-diff-label">iterations</div>
        <div className="iter-diff-subtle">
          {iterations.length} drafts · comparing v{aIdx + 1} and v{bIdx + 1}
        </div>
      </div>

      <div className="iter-diff-panes">
        <IterationPane
          side="a"
          iterations={iterations}
          selectedIdx={aIdx}
          onSelect={setAIdx}
          iteration={iterA}
          changes={changes}
        />
        <IterationPane
          side="b"
          iterations={iterations}
          selectedIdx={bIdx}
          onSelect={setBIdx}
          iteration={iterB}
          changes={changes}
        />
      </div>
    </section>
  );
}

function IterationPane({
  side,
  iterations,
  selectedIdx,
  onSelect,
  iteration,
  changes,
}: {
  side: "a" | "b";
  iterations: RefineIteration[];
  selectedIdx: number;
  onSelect: (idx: number) => void;
  iteration: RefineIteration;
  changes: Change[];
}) {
  return (
    <div className={`iter-diff-pane iter-diff-pane-${side}`}>
      <div className="iter-diff-pane-head">
        <label className="iter-diff-picker">
          <span className="iter-diff-picker-label">
            {side === "a" ? "A" : "B"}
          </span>
          <select
            className="iter-diff-select"
            value={selectedIdx}
            onChange={(e) => onSelect(Number(e.target.value))}
          >
            {iterations.map((it, idx) => (
              <option key={it.draft_page_id} value={idx}>
                v{idx + 1} · {it.draft_short_id}
              </option>
            ))}
          </select>
        </label>
        <VerdictChip verdict={iteration.verdict} />
      </div>
      <DiffBody changes={changes} showSide={side === "a" ? "removed" : "added"} />
    </div>
  );
}

function VerdictChip({
  verdict,
}: {
  verdict: RefineIterationVerdict | null;
}) {
  if (!verdict) {
    return (
      <div className="iter-diff-verdict iter-diff-verdict-missing">
        no verdict
      </div>
    );
  }
  const holdsMark = verdict.claim_holds ? "✓" : "✗";
  const holdsClass = verdict.claim_holds
    ? "iter-diff-verdict-holds"
    : "iter-diff-verdict-fails";
  return (
    <div className={`iter-diff-verdict ${holdsClass}`}>
      <span className="iter-diff-verdict-mark" aria-hidden>
        {holdsMark}
      </span>
      <span className="iter-diff-verdict-conf">
        conf {verdict.claim_confidence}
      </span>
      {verdict.dissents.length > 0 && (
        <span className="iter-diff-verdict-count" title={verdict.dissents.join("\n")}>
          {verdict.dissents.length} dissent{verdict.dissents.length === 1 ? "" : "s"}
        </span>
      )}
      {verdict.concurrences.length > 0 && (
        <span
          className="iter-diff-verdict-count"
          title={verdict.concurrences.join("\n")}
        >
          {verdict.concurrences.length} concur{verdict.concurrences.length === 1 ? "" : "s"}
        </span>
      )}
    </div>
  );
}

function DiffBody({
  changes,
  showSide,
}: {
  changes: Change[];
  showSide: "removed" | "added";
}) {
  // Each pane only needs to show one side of the diff. On the A pane we
  // render unchanged + removed lines (the "before" state); on the B pane
  // we render unchanged + added lines ("after"). Lines are rendered as
  // preformatted blocks so whitespace survives — markdown has meaningful
  // indentation. A terminal newline from diffLines is stripped per-line
  // to avoid doubling the visual gap.
  const rendered: Array<{ key: string; cls: string; text: string }> = [];
  changes.forEach((change, i) => {
    if (change.added && showSide === "removed") return;
    if (change.removed && showSide === "added") return;
    const lines = change.value.split("\n");
    // diffLines returns a trailing newline for each hunk; strip empty tail.
    if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
    const kind = change.added
      ? "added"
      : change.removed
        ? "removed"
        : "context";
    lines.forEach((line, j) => {
      rendered.push({
        key: `${i}-${j}`,
        cls: `iter-diff-line iter-diff-line-${kind}`,
        text: line.length === 0 ? "\u00A0" : line,
      });
    });
  });

  return (
    <pre className="iter-diff-body">
      {rendered.map((r) => (
        <div key={r.key} className={r.cls}>
          {r.text}
        </div>
      ))}
    </pre>
  );
}
