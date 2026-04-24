"use client";

import { useState } from "react";
import Link from "next/link";
import type { JudgmentRow } from "@/api/types.gen";
import { JudgmentDetailPanel } from "./JudgmentDetailPanel";

export function JudgmentRowsTable({ rows }: { rows: JudgmentRow[] }) {
  const [selected, setSelected] = useState<string | null>(null);

  return (
    <>
      <div style={{ overflowX: "auto", marginTop: 6 }}>
        <table className="log">
          <thead>
            <tr>
              <th>essay</th>
              <th>source_a</th>
              <th>source_b</th>
              <th>criterion</th>
              <th>judge</th>
              <th>verdict</th>
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
                <td className="versus-mono">{r.source_a}</td>
                <td className="versus-mono">{r.source_b}</td>
                <td>{r.criterion}</td>
                <td className="versus-mono">{r.judge_model}</td>
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
