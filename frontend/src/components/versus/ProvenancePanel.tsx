import type { ProvenanceSummary } from "@/api/types.gen";

const AXES: { key: keyof ProvenanceSummary; label: string }[] = [
  { key: "prefix_config_hash", label: "prefix_config_hash" },
  { key: "judge_model", label: "judge_model" },
  { key: "judge_prompt_hash", label: "judge_prompt_hash" },
  { key: "judge_version", label: "judge_version" },
  { key: "sampling_hash", label: "sampling_hash" },
];

/** Combine N per-variant ProvenanceSummary objects into one map. */
function mergeAxes(
  bundles: ProvenanceSummary[],
): Record<keyof ProvenanceSummary, Record<string, number>> {
  const out = Object.fromEntries(
    AXES.map((a) => [a.key, {}]),
  ) as Record<keyof ProvenanceSummary, Record<string, number>>;
  for (const b of bundles) {
    for (const { key } of AXES) {
      const src = b[key] ?? {};
      for (const [v, n] of Object.entries(src)) {
        out[key][v] = (out[key][v] ?? 0) + n;
      }
    }
  }
  return out;
}

/** Per-axis row of values+counts. Empty axes are skipped. Values
 *  sorted by descending count. Color-codes "rare" values (<5% of the
 *  axis total) so an operator instantly sees outliers. */
export function ProvenancePanel({ summaries }: { summaries: ProvenanceSummary[] }) {
  if (summaries.length === 0) return null;
  const merged = mergeAxes(summaries);
  return (
    <details className="prov-panel" open>
      <summary>provenance</summary>
      <dl className="prov-grid">
        {AXES.map(({ key, label }) => {
          const entries = Object.entries(merged[key]).sort((a, b) => b[1] - a[1]);
          if (entries.length === 0) return null;
          const total = entries.reduce((s, [, n]) => s + n, 0);
          return (
            <div key={key} className="prov-row">
              <dt>{label}</dt>
              <dd>
                {entries.map(([v, n]) => {
                  const share = n / total;
                  const cls = share < 0.05 ? "prov-val rare" : "prov-val";
                  return (
                    <span key={v} className={cls} title={`${n} of ${total} rows`}>
                      <code>{v}</code>{" "}
                      <span className="prov-n">{n}</span>
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
          gap: 4px;
        }
        .prov-row {
          display: grid;
          grid-template-columns: 170px 1fr;
          gap: 12px;
          align-items: baseline;
        }
        .prov-row dt {
          color: var(--color-muted);
          font-family: ui-monospace, Menlo, monospace;
          font-size: 11px;
        }
        .prov-row dd {
          margin: 0;
          display: flex; flex-wrap: wrap; gap: 8px;
        }
        .prov-val {
          font-size: 11px;
          color: var(--foreground);
        }
        .prov-val.rare {
          background: hsl(40 80% 90%);
          color: hsl(40 70% 28%);
          padding: 1px 5px;
          border-radius: 3px;
        }
        @media (prefers-color-scheme: dark) {
          .prov-val.rare { background: hsl(40 50% 16%); color: hsl(40 70% 75%); }
        }
        .prov-n { color: var(--color-muted); font-size: 10px; }
      `}</style>
    </details>
  );
}
