"use client";

import { useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { CallsForQuestion, DegreeCell } from "@/api";

type Histogram = { [key: string]: number };

export type StatsViewData = {
  pages_total: number;
  pages_by_type: { [key: string]: number };
  links_total: number;
  links_by_type: { [key: string]: number };
  degree_matrix: { [key: string]: { [key: string]: DegreeCell } };
  robustness_histogram: Histogram;
  credence_histogram: Histogram;
  calls_per_question: Array<CallsForQuestion>;
};

const TYPE_ORDER = [
  "question",
  "claim",
  "judgement",
  "source",
  "concept",
  "wiki",
];

const KNOWN_TYPES = new Set(TYPE_ORDER);

function typeVar(t: string, suffix: string): string {
  // Known page types have CSS vars; unknown ones fall back to neutral tokens.
  if (KNOWN_TYPES.has(t)) return `var(--type-${t}${suffix})`;
  if (suffix === "") return "var(--color-foreground)";
  if (suffix === "-bg-hover") return "var(--color-surface)";
  return "var(--color-border)";
}

const LINK_ORDER = [
  "child_question",
  "consideration",
  "answers",
  "same_as",
  "supporting_evidence",
  "opposing_evidence",
  "related",
  "context",
];

const CALL_TYPE_COLORS: Record<string, string> = {
  find_considerations: "var(--type-claim)",
  assess: "var(--type-judgement)",
  prioritization: "var(--type-question)",
  ingest: "var(--type-source)",
  concept_scout: "var(--type-concept)",
  concept_assess: "var(--type-concept)",
  scout: "var(--type-wiki)",
  web_research: "var(--type-wiki)",
};

const FALLBACK_PALETTE = [
  "#7a9abb",
  "#b8a46a",
  "#9388ad",
  "#6aaa9f",
  "#6fa877",
  "#8a8f96",
  "#d4943a",
  "#c87d6a",
];

function callTypeColor(callType: string, index: number): string {
  return CALL_TYPE_COLORS[callType] ?? FALLBACK_PALETTE[index % FALLBACK_PALETTE.length];
}

function formatAvg(n: number): string {
  if (n === 0) return "0";
  if (n < 0.01) return n.toFixed(3);
  if (n < 10) return n.toFixed(2);
  return n.toFixed(1);
}

function orderedTypeKeys(obj: { [key: string]: unknown }): string[] {
  const known = TYPE_ORDER.filter((t) => t in obj);
  const unknown = Object.keys(obj).filter((k) => !TYPE_ORDER.includes(k));
  return [...known, ...unknown];
}

function orderedLinkKeys(obj: { [key: string]: unknown }): string[] {
  const known = LINK_ORDER.filter((t) => t in obj);
  const unknown = Object.keys(obj).filter((k) => !LINK_ORDER.includes(k));
  return [...known, ...unknown.sort()];
}

function buildHistogramSeries(
  hist: Histogram,
  min: number,
  max: number,
): Array<{ bucket: string; n: number }> {
  const out: Array<{ bucket: string; n: number }> = [];
  for (let i = min; i <= max; i++) {
    out.push({ bucket: String(i), n: hist[String(i)] ?? 0 });
  }
  return out;
}

type CallBucket = {
  label: string;
  questions: number;
  [callType: string]: number | string;
};

// Choose inclusive bin ranges covering 0..maxVal. Zero always gets its own
// bin (questions with no calls are qualitatively different). For small ranges
// every value is its own bin; for larger ranges we pick a "nice" width so we
// end up with roughly targetBins buckets.
function computeBinRanges(maxVal: number): Array<[number, number]> {
  if (maxVal <= 0) return [[0, 0]];
  if (maxVal <= 8) {
    return Array.from(
      { length: maxVal + 1 },
      (_, i) => [i, i] as [number, number],
    );
  }
  const targetBins = 6;
  const rawWidth = maxVal / targetBins;
  const niceSteps = [2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000];
  const width =
    niceSteps.find((s) => s >= rawWidth) ?? niceSteps[niceSteps.length - 1];
  const ranges: Array<[number, number]> = [[0, 0]];
  let lo = 1;
  while (lo <= maxVal) {
    const hi = Math.min(lo + width - 1, maxVal);
    ranges.push([lo, hi]);
    lo = hi + 1;
  }
  return ranges;
}

function labelRange([lo, hi]: [number, number]): string {
  return lo === hi ? String(lo) : `${lo}–${hi}`;
}

function collectCallTypes(entries: Array<CallsForQuestion>): string[] {
  const s = new Set<string>();
  for (const e of entries) {
    for (const k of Object.keys(e.by_type)) s.add(k);
  }
  return Array.from(s).sort();
}

// Bin questions by their *effective* total — the sum of calls across the
// currently-visible call types. When a legend chip is toggled off, questions
// re-bin (and the x-axis reshapes) to reflect the filtered view.
function binCallsPerQuestion(
  entries: Array<CallsForQuestion>,
  callTypes: string[],
  hidden: Set<string>,
): CallBucket[] {
  const visible = callTypes.filter((ct) => !hidden.has(ct));

  let maxTotal = 0;
  const effectiveTotals: number[] = new Array(entries.length);
  for (let i = 0; i < entries.length; i++) {
    const e = entries[i];
    let t = 0;
    for (const ct of visible) t += e.by_type[ct] ?? 0;
    effectiveTotals[i] = t;
    if (t > maxTotal) maxTotal = t;
  }

  const ranges = computeBinRanges(maxTotal);
  const bins: CallBucket[] = ranges.map((r) => {
    const entry: CallBucket = { label: labelRange(r), questions: 0 };
    for (const ct of callTypes) entry[ct] = 0;
    return entry;
  });

  const findBinIdx = (total: number): number => {
    for (let i = 0; i < ranges.length; i++) {
      const [lo, hi] = ranges[i];
      if (total >= lo && total <= hi) return i;
    }
    return -1;
  };

  for (let i = 0; i < entries.length; i++) {
    const idx = findBinIdx(effectiveTotals[i]);
    if (idx < 0) continue;
    const bucket = bins[idx];
    bucket.questions += 1;
    // Only accumulate visible call-type counts; hidden types stay at 0 so
    // their (invisible) bar segments don't contribute to the stack height.
    const e = entries[i];
    for (const ct of visible) {
      const n = e.by_type[ct] ?? 0;
      if (n) bucket[ct] = ((bucket[ct] as number) ?? 0) + n;
    }
  }

  return bins;
}

export function StatsView({
  data,
  leadingPanel,
}: {
  data: StatsViewData;
  leadingPanel?: React.ReactNode;
}) {
  const pageTypeKeys = useMemo(
    () => orderedTypeKeys(data.pages_by_type),
    [data.pages_by_type],
  );
  const linkTypeKeys = useMemo(
    () => orderedLinkKeys(data.links_by_type),
    [data.links_by_type],
  );
  const matrixRowKeys = useMemo(
    () => orderedTypeKeys(data.degree_matrix),
    [data.degree_matrix],
  );
  const matrixColKeys = useMemo(() => {
    const seen: Record<string, true> = {};
    for (const row of Object.values(data.degree_matrix)) {
      for (const k of Object.keys(row)) seen[k] = true;
    }
    return orderedLinkKeys(seen);
  }, [data.degree_matrix]);

  const pageTypeMax = Math.max(1, ...Object.values(data.pages_by_type));
  const linkTypeMax = Math.max(1, ...Object.values(data.links_by_type));

  const robustnessSeries = useMemo(
    () => buildHistogramSeries(data.robustness_histogram, 1, 5),
    [data.robustness_histogram],
  );
  const credenceSeries = useMemo(
    () => buildHistogramSeries(data.credence_histogram, 1, 9),
    [data.credence_histogram],
  );
  const callTypes = useMemo(
    () => collectCallTypes(data.calls_per_question),
    [data.calls_per_question],
  );
  const [hiddenCallTypes, setHiddenCallTypes] = useState<Set<string>>(
    () => new Set(),
  );
  const callsBins = useMemo(
    () => binCallsPerQuestion(data.calls_per_question, callTypes, hiddenCallTypes),
    [data.calls_per_question, callTypes, hiddenCallTypes],
  );
  const visibleCallTypes = useMemo(
    () => callTypes.filter((ct) => !hiddenCallTypes.has(ct)),
    [callTypes, hiddenCallTypes],
  );
  const toggleCallType = (ct: string) => {
    setHiddenCallTypes((prev) => {
      const next = new Set(prev);
      if (next.has(ct)) next.delete(ct);
      else next.add(ct);
      return next;
    });
  };
  const anyHidden = hiddenCallTypes.size > 0;
  const toggleAllCallTypes = () => {
    setHiddenCallTypes(anyHidden ? new Set() : new Set(callTypes));
  };
  const totalQuestions = data.calls_per_question.length;
  const totalCalls = data.calls_per_question.reduce((s, e) => s + e.total, 0);

  const allHistEmpty =
    Object.keys(data.robustness_histogram).length === 0 &&
    Object.keys(data.credence_histogram).length === 0;

  return (
    <div className="stats-view">
      <style>{`
        .stats-view {
          display: flex;
          flex-direction: column;
          gap: 1.5rem;
          font-family: var(--font-geist-sans), system-ui, sans-serif;
        }

        .stats-view .panel {
          border: 1px solid var(--color-border);
          background: var(--color-background);
          padding: 1.1rem 1.25rem 1.25rem 1.25rem;
          position: relative;
          animation: panelFadeIn 0.3s ease both;
        }
        @keyframes panelFadeIn {
          from { opacity: 0; transform: translateY(4px); }
          to { opacity: 1; transform: translateY(0); }
        }

        .stats-view .panel-label {
          font-size: 0.68rem;
          font-weight: 600;
          letter-spacing: 0.14em;
          text-transform: uppercase;
          color: var(--color-muted);
          font-family: var(--font-geist-mono), monospace;
          margin-bottom: 0.9rem;
        }

        .summary-row {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr));
          gap: 1px;
          background: var(--color-border);
          border: 1px solid var(--color-border);
        }
        .summary-cell {
          background: var(--color-background);
          padding: 1.1rem 1.25rem;
          display: flex;
          flex-direction: column;
          gap: 0.35rem;
        }
        .summary-cell .label {
          font-size: 0.68rem;
          font-weight: 600;
          letter-spacing: 0.12em;
          text-transform: uppercase;
          color: var(--color-muted);
          font-family: var(--font-geist-mono), monospace;
        }
        .summary-cell .value {
          font-size: 2rem;
          font-weight: 700;
          letter-spacing: -0.02em;
          font-variant-numeric: tabular-nums;
          line-height: 1.1;
        }
        .summary-cell .sub {
          font-size: 0.72rem;
          color: var(--color-muted);
          font-family: var(--font-geist-mono), monospace;
          letter-spacing: 0.02em;
        }

        .bar-list {
          display: flex;
          flex-direction: column;
          gap: 0.45rem;
        }
        .bar-row {
          display: grid;
          grid-template-columns: 7rem 1fr 3rem;
          align-items: center;
          gap: 0.75rem;
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.75rem;
        }
        .bar-row .name {
          letter-spacing: 0.02em;
          text-align: right;
          opacity: 0.9;
        }
        .bar-row .track {
          position: relative;
          height: 0.9rem;
          background: var(--color-background);
          border: 1px solid var(--color-border);
        }
        .bar-row .fill {
          position: absolute;
          top: 0;
          left: 0;
          bottom: 0;
          transform-origin: left;
          animation: barGrow 0.5s cubic-bezier(0.2, 0.8, 0.2, 1) both;
        }
        .bar-row .link-fill {
          background: repeating-linear-gradient(
            45deg,
            var(--color-border) 0 2px,
            transparent 2px 6px
          );
          border-right: 2px solid var(--color-accent);
        }
        @keyframes barGrow {
          from { transform: scaleX(0); }
          to { transform: scaleX(1); }
        }
        .bar-row .count {
          font-variant-numeric: tabular-nums;
          text-align: right;
          opacity: 0.75;
        }

        .matrix {
          overflow-x: auto;
        }
        .matrix table {
          border-collapse: collapse;
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.72rem;
          width: 100%;
        }
        .matrix th, .matrix td {
          border: 1px solid var(--color-border);
          padding: 0.4rem 0.55rem;
          text-align: left;
          white-space: nowrap;
        }
        .matrix th {
          font-weight: 600;
          letter-spacing: 0.03em;
          color: var(--color-muted);
          text-transform: lowercase;
          background: var(--color-surface);
          font-size: 0.68rem;
        }
        .matrix td.row-header {
          font-weight: 600;
          background: var(--color-surface);
          color: var(--color-foreground);
        }
        .matrix .cell-empty {
          color: var(--color-dim);
          opacity: 0.4;
          text-align: center;
        }
        .matrix .cell-content {
          display: flex;
          flex-direction: column;
          gap: 0.15rem;
          font-variant-numeric: tabular-nums;
        }
        .matrix .cell-out {
          color: var(--color-foreground);
        }
        .matrix .cell-in {
          color: var(--color-muted);
          opacity: 0.75;
        }
        .matrix .arrow {
          display: inline-block;
          width: 0.7rem;
          opacity: 0.45;
          font-weight: 400;
        }

        .two-col-histograms {
          display: grid;
          grid-template-columns: 1fr;
          gap: 1rem;
        }
        @media (min-width: 52rem) {
          .two-col-histograms {
            grid-template-columns: 1fr 1fr;
          }
        }

        .hist-wrap {
          height: 14rem;
          margin-top: 0.2rem;
        }
        .hist-empty {
          padding: 2.5rem 0;
          text-align: center;
          color: var(--color-muted);
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.75rem;
          letter-spacing: 0.03em;
        }
        .hist-caption {
          font-size: 0.68rem;
          color: var(--color-muted);
          font-family: var(--font-geist-mono), monospace;
          letter-spacing: 0.03em;
          margin-top: 0.4rem;
        }

        .calls-wrap {
          height: 16rem;
          margin-top: 0.2rem;
        }
        .legend-row {
          display: flex;
          flex-wrap: wrap;
          gap: 0.4rem 0.5rem;
          margin-top: 0.6rem;
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.68rem;
          letter-spacing: 0.02em;
        }
        .legend-chip {
          display: inline-flex;
          align-items: center;
          gap: 0.35rem;
          padding: 0.25rem 0.5rem;
          background: transparent;
          border: 1px solid var(--color-border);
          color: var(--color-foreground);
          font-family: inherit;
          font-size: inherit;
          letter-spacing: inherit;
          cursor: pointer;
          user-select: none;
          transition: all 0.12s ease;
        }
        .legend-chip:hover {
          border-color: var(--color-accent);
        }
        .legend-chip.hidden {
          color: var(--color-muted);
          opacity: 0.55;
          text-decoration: line-through;
          text-decoration-thickness: 1px;
        }
        .legend-chip.legend-all {
          color: var(--color-muted);
          text-transform: uppercase;
          letter-spacing: 0.08em;
          font-size: 0.62rem;
          padding: 0.25rem 0.55rem;
          margin-right: 0.3rem;
        }
        .legend-chip.legend-all:hover {
          color: var(--color-foreground);
        }
        .legend-swatch {
          display: inline-block;
          width: 0.7rem;
          height: 0.7rem;
          vertical-align: -1px;
          border: 1px solid var(--color-border);
        }

        .empty-panel {
          padding: 2rem 0;
          text-align: center;
          color: var(--color-muted);
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.75rem;
          letter-spacing: 0.03em;
        }

        .tooltip-box {
          background: var(--color-background);
          border: 1px solid var(--color-border);
          padding: 0.4rem 0.6rem;
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.7rem;
          letter-spacing: 0.02em;
          box-shadow: 0 4px 16px rgba(0, 0, 0, 0.08);
        }
        .tooltip-box .tip-label {
          color: var(--color-muted);
          text-transform: uppercase;
          font-size: 0.62rem;
          letter-spacing: 0.08em;
          margin-bottom: 0.2rem;
        }
        .tooltip-box .tip-row {
          display: flex;
          align-items: center;
          gap: 0.4rem;
          font-variant-numeric: tabular-nums;
        }
        .tooltip-box .tip-swatch {
          width: 0.55rem;
          height: 0.55rem;
          border: 1px solid var(--color-border);
        }
      `}</style>

      {leadingPanel}

      <div className="summary-row">
        <div className="summary-cell">
          <span className="label">Pages</span>
          <span className="value">{data.pages_total.toLocaleString()}</span>
          <span className="sub">
            {pageTypeKeys.length} type{pageTypeKeys.length === 1 ? "" : "s"}
          </span>
        </div>
        <div className="summary-cell">
          <span className="label">Links</span>
          <span className="value">{data.links_total.toLocaleString()}</span>
          <span className="sub">
            {linkTypeKeys.length} type{linkTypeKeys.length === 1 ? "" : "s"}
          </span>
        </div>
        <div className="summary-cell">
          <span className="label">Questions</span>
          <span className="value">{totalQuestions.toLocaleString()}</span>
          <span className="sub">
            {totalCalls.toLocaleString()} call{totalCalls === 1 ? "" : "s"}
          </span>
        </div>
      </div>

      <div className="panel">
        <div className="panel-label">Pages by type</div>
        {pageTypeKeys.length === 0 ? (
          <div className="empty-panel">no pages</div>
        ) : (
          <div className="bar-list">
            {pageTypeKeys.map((t, i) => {
              const n = data.pages_by_type[t];
              const pct = (n / pageTypeMax) * 100;
              return (
                <div key={t} className="bar-row">
                  <span className="name" style={{ color: typeVar(t, "") }}>
                    {t}
                  </span>
                  <div className="track">
                    <div
                      className="fill"
                      style={{
                        width: `${pct}%`,
                        background: typeVar(t, "-border"),
                        borderRight: `2px solid ${typeVar(t, "")}`,
                        animationDelay: `${i * 40}ms`,
                      }}
                    />
                  </div>
                  <span className="count">{n}</span>
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div className="panel">
        <div className="panel-label">Links by type</div>
        {linkTypeKeys.length === 0 ? (
          <div className="empty-panel">no links</div>
        ) : (
          <div className="bar-list">
            {linkTypeKeys.map((t, i) => {
              const n = data.links_by_type[t];
              const pct = (n / linkTypeMax) * 100;
              return (
                <div key={t} className="bar-row">
                  <span className="name">{t}</span>
                  <div className="track">
                    <div
                      className="fill link-fill"
                      style={{
                        width: `${pct}%`,
                        animationDelay: `${i * 40}ms`,
                      }}
                    />
                  </div>
                  <span className="count">{n}</span>
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div className="panel">
        <div className="panel-label">
          Degree matrix &nbsp;
          <span style={{ opacity: 0.7, fontWeight: 400 }}>
            avg per page of type
          </span>
        </div>
        {matrixRowKeys.length === 0 ? (
          <div className="empty-panel">no edges</div>
        ) : (
          <div className="matrix">
            <table>
              <thead>
                <tr>
                  <th />
                  {matrixColKeys.map((c) => (
                    <th key={c}>{c}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {matrixRowKeys.map((r) => (
                  <tr key={r}>
                    <td
                      className="row-header"
                      style={{ color: typeVar(r, "") }}
                    >
                      {r}
                    </td>
                    {matrixColKeys.map((c) => {
                      const cell: DegreeCell | undefined =
                        data.degree_matrix[r]?.[c];
                      if (!cell || (cell.avg_out === 0 && cell.avg_in === 0)) {
                        return (
                          <td key={c} className="cell-empty">
                            ·
                          </td>
                        );
                      }
                      return (
                        <td key={c}>
                          <div className="cell-content">
                            <span className="cell-out">
                              <span className="arrow">↑</span>
                              {formatAvg(cell.avg_out)}
                            </span>
                            <span className="cell-in">
                              <span className="arrow">↓</span>
                              {formatAvg(cell.avg_in)}
                            </span>
                          </div>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="two-col-histograms">
        <div className="panel">
          <div className="panel-label">Credence</div>
          {Object.keys(data.credence_histogram).length === 0 ? (
            <div className="hist-empty">no credence-scored pages</div>
          ) : (
            <>
              <div className="hist-wrap">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart
                    data={credenceSeries}
                    margin={{ top: 8, right: 8, left: -16, bottom: 0 }}
                  >
                    <CartesianGrid
                      strokeDasharray="2 2"
                      stroke="var(--color-border)"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="bucket"
                      tick={{
                        fontSize: 10,
                        fontFamily: "var(--font-geist-mono), monospace",
                        fill: "var(--color-muted)",
                      }}
                      axisLine={{ stroke: "var(--color-border)" }}
                      tickLine={false}
                    />
                    <YAxis
                      tick={{
                        fontSize: 10,
                        fontFamily: "var(--font-geist-mono), monospace",
                        fill: "var(--color-muted)",
                      }}
                      allowDecimals={false}
                      axisLine={{ stroke: "var(--color-border)" }}
                      tickLine={false}
                    />
                    <Tooltip
                      cursor={{ fill: "var(--color-surface)" }}
                      content={<HistTooltip label="credence" />}
                    />
                    <Bar
                      dataKey="n"
                      fill="var(--type-claim-bg-hover)"
                      stroke="var(--type-claim)"
                      strokeWidth={1}
                      isAnimationActive
                      animationDuration={500}
                    />
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <div className="hist-caption">bins 1–9 · claims and judgements</div>
            </>
          )}
        </div>

        <div className="panel">
          <div className="panel-label">Robustness</div>
          {Object.keys(data.robustness_histogram).length === 0 ? (
            <div className="hist-empty">no robustness-scored pages</div>
          ) : (
            <>
              <div className="hist-wrap">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart
                    data={robustnessSeries}
                    margin={{ top: 8, right: 8, left: -16, bottom: 0 }}
                  >
                    <CartesianGrid
                      strokeDasharray="2 2"
                      stroke="var(--color-border)"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="bucket"
                      tick={{
                        fontSize: 10,
                        fontFamily: "var(--font-geist-mono), monospace",
                        fill: "var(--color-muted)",
                      }}
                      axisLine={{ stroke: "var(--color-border)" }}
                      tickLine={false}
                    />
                    <YAxis
                      tick={{
                        fontSize: 10,
                        fontFamily: "var(--font-geist-mono), monospace",
                        fill: "var(--color-muted)",
                      }}
                      allowDecimals={false}
                      axisLine={{ stroke: "var(--color-border)" }}
                      tickLine={false}
                    />
                    <Tooltip
                      cursor={{ fill: "var(--color-surface)" }}
                      content={<HistTooltip label="robustness" />}
                    />
                    <Bar
                      dataKey="n"
                      fill="var(--type-judgement-bg-hover)"
                      stroke="var(--type-judgement)"
                      strokeWidth={1}
                      isAnimationActive
                      animationDuration={500}
                    />
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <div className="hist-caption">bins 1–5 · claims and judgements</div>
            </>
          )}
        </div>
      </div>

      {allHistEmpty && null}

      <div className="panel">
        <div className="panel-label">
          Calls per question &nbsp;
          <span style={{ opacity: 0.7, fontWeight: 400 }}>
            questions binned by total calls, stacked by call type
          </span>
        </div>
        {callTypes.length === 0 ? (
          <div className="empty-panel">no calls recorded</div>
        ) : (
          <>
            <div className="calls-wrap">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  data={callsBins}
                  margin={{ top: 8, right: 8, left: -16, bottom: 0 }}
                >
                  <CartesianGrid
                    strokeDasharray="2 2"
                    stroke="var(--color-border)"
                    vertical={false}
                  />
                  <XAxis
                    dataKey="label"
                    tick={{
                      fontSize: 10,
                      fontFamily: "var(--font-geist-mono), monospace",
                      fill: "var(--color-muted)",
                    }}
                    axisLine={{ stroke: "var(--color-border)" }}
                    tickLine={false}
                  />
                  <YAxis
                    tick={{
                      fontSize: 10,
                      fontFamily: "var(--font-geist-mono), monospace",
                      fill: "var(--color-muted)",
                    }}
                    allowDecimals={false}
                    axisLine={{ stroke: "var(--color-border)" }}
                    tickLine={false}
                  />
                  <Tooltip
                    cursor={{ fill: "var(--color-surface)" }}
                    content={<CallsTooltip callTypes={visibleCallTypes} />}
                  />
                  <Legend content={() => null} />
                  {visibleCallTypes.map((ct) => {
                    const i = callTypes.indexOf(ct);
                    return (
                      <Bar
                        key={ct}
                        dataKey={ct}
                        stackId="calls"
                        fill={callTypeColor(ct, i)}
                        isAnimationActive
                        animationDuration={500}
                      >
                        {callsBins.map((_, idx) => (
                          <Cell key={idx} />
                        ))}
                      </Bar>
                    );
                  })}
                </BarChart>
              </ResponsiveContainer>
            </div>
            <div className="legend-row">
              <button
                type="button"
                className="legend-chip legend-all"
                onClick={toggleAllCallTypes}
                title={anyHidden ? "Show all call types" : "Hide all call types"}
              >
                {anyHidden ? "show all" : "hide all"}
              </button>
              {callTypes.map((ct, i) => {
                const hidden = hiddenCallTypes.has(ct);
                return (
                  <button
                    key={ct}
                    type="button"
                    className={`legend-chip${hidden ? " hidden" : ""}`}
                    onClick={() => toggleCallType(ct)}
                    title={hidden ? `Show ${ct}` : `Hide ${ct}`}
                  >
                    <span
                      className="legend-swatch"
                      style={{
                        background: hidden
                          ? "transparent"
                          : callTypeColor(ct, i),
                        borderColor: callTypeColor(ct, i),
                      }}
                    />
                    {ct}
                  </button>
                );
              })}
            </div>
            <div className="hist-caption">
              x-axis: total calls dispatched against the question ·{" "}
              click a call type to toggle
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function HistTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: Array<{ value: number; color: string; payload: { bucket: string } }>;
  label?: string;
}) {
  if (!active || !payload || payload.length === 0) return null;
  const item = payload[0];
  return (
    <div className="tooltip-box">
      <div className="tip-label">{label}</div>
      <div className="tip-row">
        <span className="tip-swatch" style={{ background: item.color }} />
        bucket {item.payload.bucket} · {item.value}
      </div>
    </div>
  );
}

function CallsTooltip({
  active,
  payload,
  label,
  callTypes,
}: {
  active?: boolean;
  payload?: Array<{
    value: number;
    color: string;
    dataKey: string;
    payload: CallBucket;
  }>;
  label?: string;
  callTypes: string[];
}) {
  if (!active || !payload || payload.length === 0) return null;
  const bucket = payload[0].payload;
  const nonZero = callTypes.filter((ct) => (bucket[ct] as number) > 0);
  return (
    <div className="tooltip-box">
      <div className="tip-label">
        {label} call{label === "1" ? "" : "s"} · {bucket.questions} question
        {bucket.questions === 1 ? "" : "s"}
      </div>
      {nonZero.map((ct) => {
        const item = payload.find((p) => p.dataKey === ct);
        if (!item) return null;
        return (
          <div key={ct} className="tip-row">
            <span className="tip-swatch" style={{ background: item.color }} />
            {ct} · {item.value}
          </div>
        );
      })}
    </div>
  );
}
