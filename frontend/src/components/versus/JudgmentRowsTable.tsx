"use client";

import { useState } from "react";
import Link from "next/link";
import type { JudgmentRow } from "@/api/types.gen";
import { JudgmentDetailPanel } from "./JudgmentDetailPanel";

/** Workflow segment from a compound judge_model display string, e.g.
 *  `judge_pair/two_phase:claude-opus-4-7:c12345678` → "two_phase". Returns
 *  null when the string doesn't match the new shape (legacy rows). */
function workflowOfJudge(judgeModel: string | null | undefined): string | null {
  if (!judgeModel) return null;
  const m = judgeModel.match(/^[^/]+\/([^:]+):/);
  return m ? m[1] : null;
}

export function JudgmentRowsTable({ rows }: { rows: JudgmentRow[] }) {
  const [selected, setSelected] = useState<string | null>(null);

  return (
    <>
      <div style={{ overflowX: "auto", marginTop: 6 }}>
        <table className="log">
          <thead>
            <tr>
              <th>essay</th>
              <th title="prefix_config_hash">prefix</th>
              <th title="alphabetical canonical (dedup-key) ordering">source_a</th>
              <th title="alphabetical canonical (dedup-key) ordering">source_b</th>
              <th title="what the judge saw as Continuation A">shown_A</th>
              <th title="what the judge saw as Continuation B">shown_B</th>
              <th>criterion</th>
              <th>judge</th>
              <th title="verdict letter refers to display order (shown_A / shown_B)">verdict</th>
              <th>winner</th>
              <th>preference</th>
              <th>ts</th>
              <th>flags</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr
                key={r.key || `${r.essay_id}|${r.judge_model}|${r.criterion}|${r.source_a}|${r.source_b}`}
                className={`judgment-row-clickable${r.is_rumil ? " is-rumil" : ""}${r.contamination_note ? " is-contam" : ""}${r.stale ? " is-stale" : ""}${r.orphaned ? " is-orphaned" : ""}`}
                onClick={() => r.key && setSelected(r.key)}
                title={r.key ? "Click to inspect" : "Legacy row without dedup key"}
              >
                <td className="versus-mono">
                  <Link
                    href={`/versus/inspect?essay=${encodeURIComponent(r.essay_id)}`}
                    onClick={(e) => e.stopPropagation()}
                  >
                    {r.essay_id}
                  </Link>
                </td>
                <td
                  className="versus-mono versus-muted"
                  style={{ fontSize: 11 }}
                  title={r.prefix_config_hash}
                >
                  {r.prefix_config_hash.slice(0, 8)}
                </td>
                <td className="versus-mono">{r.source_a}</td>
                <td className="versus-mono">{r.source_b}</td>
                <td className="versus-mono">{r.display_first}</td>
                <td className="versus-mono">{r.display_second}</td>
                <td>{r.criterion}</td>
                <td className="versus-mono" title={r.judge_model}>
                  {r.judge_model_id}
                  {(() => {
                    const wf = workflowOfJudge(r.judge_model);
                    return wf ? (
                      <span
                        className="versus-pill"
                        style={{ fontSize: 10, marginLeft: 6 }}
                        title={`workflow: ${wf}`}
                      >
                        {wf}
                      </span>
                    ) : null;
                  })()}
                </td>
                <td>{r.verdict}</td>
                <td className="versus-mono">{r.winner}</td>
                <td style={{ fontSize: 11 }}>{r.preference_label ?? ""}</td>
                <td className="versus-muted">{r.ts}</td>
                <td>
                  {r.stale && (
                    <span className="versus-pill stale" title="Judgment references an older essay version">
                      stale
                    </span>
                  )}
                  {r.orphaned && (
                    <span className="versus-pill stale" title="No matching completion row for source_a / source_b at the current prefix_config_hash">
                      orphan
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <JudgmentDetailPanel selectedKey={selected} onClose={() => setSelected(null)} />
    </>
  );
}
