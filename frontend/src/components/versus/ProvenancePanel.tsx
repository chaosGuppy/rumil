import type { ProvenanceSummary } from "@/api/types.gen";

type AxisKey = "prefix_config_hash" | "judge_model" | "judge_prompt_hash" | "judge_version" | "sampling_hash";

const AXES: { key: AxisKey; label: string }[] = [
  { key: "prefix_config_hash", label: "prefix_config_hash" },
  { key: "judge_model", label: "judge_model" },
  { key: "judge_prompt_hash", label: "judge_prompt_hash" },
  { key: "judge_version", label: "judge_version" },
  { key: "sampling_hash", label: "sampling_hash" },
];

/** Combine N per-variant ProvenanceSummary objects into one map per
 *  axis (counts add) plus a unified ``current`` set (union). */
function mergeAxes(bundles: ProvenanceSummary[]): {
  counts: Record<AxisKey, Record<string, number>>;
  current: Record<AxisKey, Set<string>>;
} {
  const counts = Object.fromEntries(AXES.map((a) => [a.key, {}])) as Record<
    AxisKey,
    Record<string, number>
  >;
  const current = Object.fromEntries(AXES.map((a) => [a.key, new Set<string>()])) as Record<
    AxisKey,
    Set<string>
  >;
  for (const b of bundles) {
    for (const { key } of AXES) {
      const src = b[key] ?? {};
      for (const [v, n] of Object.entries(src)) {
        counts[key][v] = (counts[key][v] ?? 0) + n;
      }
      const cur = b.current?.[key] ?? [];
      for (const v of cur) current[key].add(v);
    }
  }
  return { counts, current };
}

/** Highlight values not in the mainline ``current`` set per axis. The
 *  rule used to be "<5% of the axis is rare" but that hid the real
 *  case (97% of rows on a stale prompt_hash with 3% on the current
 *  one). Anchoring to current flips the highlight to where the
 *  problem actually is. Empty ``current[axis]`` means "we don't know
 *  current here yet" — render values neutrally. */
export function ProvenancePanel({ summaries }: { summaries: ProvenanceSummary[] }) {
  if (summaries.length === 0) return null;
  const { counts, current } = mergeAxes(summaries);
  return (
    <details className="prov-panel" open>
      <summary>provenance</summary>
      <dl className="prov-grid">
        {AXES.map(({ key, label }) => {
          const entries = Object.entries(counts[key]).sort((a, b) => b[1] - a[1]);
          if (entries.length === 0) return null;
          const total = entries.reduce((s, [, n]) => s + n, 0);
          const knownCurrent = current[key].size > 0;
          let currentRows = 0;
          for (const [v, n] of entries) if (current[key].has(v)) currentRows += n;
          return (
            <div key={key} className="prov-row">
              <dt>
                {label}
                {knownCurrent && (
                  <div className="prov-axis-share">
                    {Math.round((currentRows / total) * 100)}% current
                  </div>
                )}
              </dt>
              <dd>
                {entries.map(([v, n]) => {
                  const isCurrent = current[key].has(v);
                  const isStale = knownCurrent && !isCurrent;
                  const share = (n / total) * 100;
                  const cls = isStale ? "prov-val stale" : isCurrent ? "prov-val current" : "prov-val";
                  const title = isStale
                    ? `${n} of ${total} rows · NOT in mainline current set`
                    : isCurrent
                      ? `${n} of ${total} rows · current/mainline value`
                      : `${n} of ${total} rows`;
                  return (
                    <span key={v} className={cls} title={title}>
                      <code>{v}</code>
                      <span className="prov-n">
                        {n} <span className="prov-share">({share.toFixed(1)}%)</span>
                      </span>
                    </span>
                  );
                })}
              </dd>
            </div>
          );
        })}
      </dl>
      <style>{`
        .prov-panel {
          margin: 6px 0 14px;
          font-size: 12px;
          padding: 0 12px;
          border: 1px solid var(--color-border);
          border-radius: 4px;
          background: var(--color-surface);
        }
        .prov-panel > summary {
          padding: 8px 0;
          cursor: pointer;
          color: var(--color-muted);
          user-select: none;
          list-style: none;
          font-size: 11px;
          text-transform: uppercase;
          letter-spacing: 0.06em;
        }
        .prov-panel > summary::-webkit-details-marker { display: none; }
        .prov-panel > summary::before {
          content: "▸"; display: inline-block; margin-right: 6px;
          transition: transform 120ms ease;
        }
        .prov-panel[open] > summary::before { transform: rotate(90deg); }
        .prov-grid {
          margin: 0 0 10px;
          display: flex;
          flex-direction: column;
          gap: 6px;
        }
        .prov-row {
          display: grid;
          grid-template-columns: 200px 1fr;
          gap: 12px;
          align-items: start;
        }
        .prov-row dt {
          color: var(--color-muted);
          font-family: ui-monospace, Menlo, monospace;
          font-size: 11px;
        }
        .prov-axis-share {
          font-family: var(--font-geist-sans), -apple-system, system-ui, sans-serif;
          font-size: 10px;
          margin-top: 2px;
          color: var(--color-muted);
          text-transform: uppercase;
          letter-spacing: 0.04em;
        }
        .prov-row dd {
          margin: 0;
          display: flex; flex-wrap: wrap; gap: 6px;
        }
        .prov-val {
          font-size: 11px;
          color: var(--foreground);
          padding: 1px 6px;
          border-radius: 3px;
          border: 1px solid transparent;
        }
        .prov-val.current {
          border-color: hsl(140 30% 78%);
        }
        .prov-val.stale {
          background: hsl(28 85% 90%);
          color: hsl(28 80% 28%);
          border-color: hsl(28 70% 65%);
        }
        @media (prefers-color-scheme: dark) {
          .prov-val.current { border-color: hsl(140 25% 30%); }
          .prov-val.stale {
            background: hsl(28 50% 18%);
            color: hsl(28 70% 78%);
            border-color: hsl(28 40% 35%);
          }
        }
        .prov-n { color: var(--color-muted); font-size: 10px; margin-left: 4px; }
        .prov-share { font-variant-numeric: tabular-nums; }
      `}</style>
    </details>
  );
}
