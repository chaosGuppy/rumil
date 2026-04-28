import type { ProvenanceAxis, ProvenanceSummary } from "@/api/types.gen";

const AXIS_ORDER = [
  "prefix_config_hash",
  "judge_path",
  "judge_base_model",
  "judge_dimension",
  "judge_workspace_id",
  "judge_prompt_hash",
  "judge_sampling_hash",
  "judge_tool_hash",
  "judge_pair_hash",
  "judge_closer_hash",
  "judge_budget",
  "judge_code_fingerprint",
  "judge_workspace_state_hash",
  "config_hash",
];

/** Combine per-variant ProvenanceSummary objects into one merged
 *  axes map. Counts add; ``current_values`` and ``value_labels``
 *  union; ``description`` is taken from whichever bundle exposed it
 *  first (they're identical by construction). */
function mergeAxes(bundles: ProvenanceSummary[]): Record<string, ProvenanceAxis> {
  const merged: Record<string, ProvenanceAxis> = {};
  for (const b of bundles) {
    for (const [axis, info] of Object.entries(b.axes ?? {})) {
      const slot = merged[axis] ?? {
        description: info.description,
        counts: {},
        current_values: [],
        value_labels: {},
      };
      for (const [v, n] of Object.entries(info.counts)) {
        slot.counts[v] = (slot.counts[v] ?? 0) + n;
      }
      const cur = new Set([...slot.current_values, ...info.current_values]);
      slot.current_values = Array.from(cur);
      slot.value_labels = { ...slot.value_labels, ...info.value_labels };
      merged[axis] = slot;
    }
  }
  return merged;
}

/** Rich provenance panel: each axis carries its own description so the
 *  operator knows what the hash is computed over, and value_labels
 *  surface the underlying KV (e.g. "essay_id / variant_id") next to
 *  opaque hashes. Values not in ``current_values`` get the amber
 *  "stale" treatment; values in it get a green border. Empty
 *  current_values means "we don't know mainline for this axis yet" —
 *  those values render neutrally. */
export function ProvenancePanel({ summaries }: { summaries: ProvenanceSummary[] }) {
  if (summaries.length === 0) return null;
  const merged = mergeAxes(summaries);
  const axes = AXIS_ORDER.filter((a) => merged[a] && Object.keys(merged[a].counts).length > 0);
  return (
    <details className="prov-panel" open>
      <summary>provenance</summary>
      <dl className="prov-grid">
        {axes.map((axis) => {
          const info = merged[axis];
          const entries = Object.entries(info.counts).sort((a, b) => b[1] - a[1]);
          const total = entries.reduce((s, [, n]) => s + n, 0);
          const currentSet = new Set(info.current_values);
          const knownCurrent = currentSet.size > 0;
          let mainlineRows = 0;
          let mainlineDistinct = 0;
          for (const [v, n] of entries) {
            if (currentSet.has(v)) {
              mainlineRows += n;
              mainlineDistinct += 1;
            }
          }
          return (
            <div key={axis} className="prov-row">
              <dt>
                <div className="prov-axis-name">{axis}</div>
                <div className="prov-axis-desc">{info.description}</div>
                {knownCurrent && (
                  <div className="prov-axis-share">
                    {mainlineRows} of {total} rows in mainline · {mainlineDistinct} of{" "}
                    {entries.length} values
                  </div>
                )}
                {!knownCurrent && (
                  <div className="prov-axis-share muted">
                    no mainline set declared
                  </div>
                )}
              </dt>
              <dd>
                {entries.map(([v, n]) => {
                  const isCurrent = currentSet.has(v);
                  const isStale = knownCurrent && !isCurrent;
                  const share = (n / total) * 100;
                  const cls = isStale ? "prov-val stale" : isCurrent ? "prov-val current" : "prov-val";
                  const label = info.value_labels[v];
                  const baseTitle = label
                    ? `${label} · ${n} of ${total} rows`
                    : `${n} of ${total} rows`;
                  const title = isStale
                    ? `${baseTitle} · NOT in mainline`
                    : isCurrent
                      ? `${baseTitle} · current/mainline`
                      : baseTitle;
                  return (
                    <span key={v} className={cls} title={title}>
                      <code>{v}</code>
                      {label && <span className="prov-label">{label}</span>}
                      <span className="prov-n">
                        {n}
                        <span className="prov-share"> ({share.toFixed(1)}%)</span>
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
          gap: 12px;
        }
        .prov-row {
          display: grid;
          grid-template-columns: 240px 1fr;
          gap: 14px;
          align-items: start;
        }
        .prov-row dt {
          display: flex; flex-direction: column; gap: 2px;
        }
        .prov-axis-name {
          font-family: ui-monospace, Menlo, monospace;
          font-size: 11px;
          color: var(--foreground);
          font-weight: 600;
        }
        .prov-axis-desc {
          font-size: 11px;
          color: var(--color-muted);
          line-height: 1.35;
        }
        .prov-axis-share {
          font-family: var(--font-geist-sans), -apple-system, system-ui, sans-serif;
          font-size: 10px;
          margin-top: 2px;
          color: var(--color-muted);
          text-transform: uppercase;
          letter-spacing: 0.04em;
        }
        .prov-axis-share.muted { opacity: 0.6; font-style: italic; }
        .prov-row dd {
          margin: 0;
          display: flex; flex-wrap: wrap; gap: 6px;
        }
        .prov-val {
          display: inline-flex; align-items: baseline; gap: 4px;
          font-size: 11px;
          color: var(--foreground);
          padding: 1px 6px;
          border-radius: 3px;
          border: 1px solid transparent;
          line-height: 1.5;
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
        .prov-label {
          color: var(--color-muted);
          font-size: 10px;
          font-style: italic;
          padding-left: 2px;
        }
        .prov-n { color: var(--color-muted); font-size: 10px; margin-left: 4px; }
        .prov-share { font-variant-numeric: tabular-nums; }
      `}</style>
    </details>
  );
}
