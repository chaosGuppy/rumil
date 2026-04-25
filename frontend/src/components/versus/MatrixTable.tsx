import Link from "next/link";
import type { Cell, GenJudgeCell, JudgeLabel } from "@/api/types.gen";
import { JudgeHeader, shortName } from "./JudgeHeader";

function cellKey(g: string, j: string): string {
  return `${g}\t${j}`;
}

export function buildCellMap(cells: GenJudgeCell[]): Map<string, Cell> {
  const m = new Map<string, Cell>();
  for (const c of cells) m.set(cellKey(c.gen_model, c.judge_model), c.cell);
  return m;
}

// Below this many judgments per cell, the cell is rendered as a low-confidence
// marker (desaturated bg, italic, leading "~") so a reader doesn't read 67%
// off three judgments as if it meant the same thing as 67% off thirty.
const SMALL_N_THRESHOLD = 5;

function formatPct(pct: number): string {
  return `${Math.round(pct * 100)}`;
}

function formatCiPct(lo: number, hi: number): string {
  return `${Math.round(lo * 100)}–${Math.round(hi * 100)}%`;
}

function cellTooltip(c: Cell): string {
  if (c.pct === null || c.pct === undefined) return "no data";
  const n = c.n;
  const ci =
    c.ci_lo !== null && c.ci_lo !== undefined && c.ci_hi !== null && c.ci_hi !== undefined
      ? ` — 95% CI: ${formatCiPct(c.ci_lo, c.ci_hi)}`
      : "";
  const tiePct = c.tie_frac !== null && c.tie_frac !== undefined ? ` (${Math.round(c.tie_frac * 100)}% ties)` : "";
  const lowN = n < SMALL_N_THRESHOLD ? ` · low n (<${SMALL_N_THRESHOLD}), read with caution` : "";
  const drill = "\nClick to filter the judgments table below.";
  return `${Math.round(c.pct * 100)}% · n=${n} (${c.wins}W / ${c.ties}T / ${c.losses}L)${tiePct}${ci}${lowN}${drill}`;
}

function buildFilterHref(params: {
  gen: string;
  judge: string;
  condition: string;
  criterion?: string | null;
  includeStale: boolean;
  includeContaminated: boolean;
}): string {
  const qs = new URLSearchParams();
  qs.set("filter_gen", params.gen);
  qs.set("filter_judge", params.judge);
  qs.set("filter_condition", params.condition);
  if (params.criterion) qs.set("filter_criterion", params.criterion);
  // Preserve the page-level toggles so clicking a cell doesn't silently flip
  // stale/contam visibility on the operator.
  qs.set("include_stale", params.includeStale ? "true" : "false");
  if (params.includeContaminated) qs.set("include_contaminated", "true");
  // Passed through to the main criterion picker — keeps the matrix's own
  // criterion setting intact after the cell click.
  if (params.criterion) qs.set("criterion", params.criterion);
  return `/versus/results?${qs.toString()}#judgments`;
}

export function MatrixTable({
  cells,
  genModels,
  judgeModels,
  judgeLabels,
  small = false,
  includeTask = true,
  condition,
  criterion,
  includeStale,
  includeContaminated,
}: {
  cells: GenJudgeCell[];
  genModels: string[];
  judgeModels: string[];
  judgeLabels: Record<string, JudgeLabel>;
  small?: boolean;
  includeTask?: boolean;
  condition?: string;
  criterion?: string | null;
  includeStale?: boolean;
  includeContaminated?: boolean;
}) {
  const map = buildCellMap(cells);
  const canLink = Boolean(condition);
  return (
    <table className={small ? "matrix-table small" : "matrix-table"}>
      <thead>
        <tr>
          <th></th>
          {judgeModels.map((j) => (
            <JudgeHeader key={j} judge={j} label={judgeLabels[j]} includeTask={includeTask} />
          ))}
        </tr>
      </thead>
      <tbody>
        {genModels.map((g) => (
          <tr key={g}>
            <th title={g}>{shortName(g)}</th>
            {judgeModels.map((j) => {
              const c = map.get(cellKey(g, j));
              if (!c) return <td key={j} className="matrix-cell-empty"></td>;
              const noData = c.pct === null || c.pct === undefined;
              if (noData) {
                return (
                  <td key={j} className="matrix-cell-empty">
                    <span className="versus-muted">—</span>
                  </td>
                );
              }
              const lowN = c.n < SMALL_N_THRESHOLD;
              const tooltip = cellTooltip(c);
              const classes = ["matrix-cell"];
              if (lowN) classes.push("low-n");
              if (canLink) classes.push("is-linked");
              const inner = (
                <>
                  {small ? (
                    <>
                      {lowN && <span className="low-n-mark">~</span>}
                      {formatPct(c.pct!)}
                    </>
                  ) : (
                    <strong>
                      {lowN && <span className="low-n-mark">~</span>}
                      {formatPct(c.pct!)}
                    </strong>
                  )}
                  <span className="n">{c.n}</span>
                </>
              );
              return (
                <td
                  key={j}
                  style={{ background: c.bg, color: c.fg }}
                  className={classes.join(" ")}
                  title={tooltip}
                >
                  {canLink ? (
                    <Link
                      href={buildFilterHref({
                        gen: g,
                        judge: j,
                        condition: condition!,
                        criterion: criterion ?? null,
                        includeStale: includeStale ?? true,
                        includeContaminated: includeContaminated ?? false,
                      })}
                      className="matrix-cell-link"
                      aria-label={`Filter rows to ${g} × ${j}, condition ${condition}${criterion ? `, criterion ${criterion}` : ""}`}
                    >
                      {inner}
                    </Link>
                  ) : (
                    inner
                  )}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
