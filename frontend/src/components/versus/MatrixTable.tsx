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

export function MatrixTable({
  cells,
  genModels,
  judgeModels,
  judgeLabels,
  small = false,
  includeTask = true,
}: {
  cells: GenJudgeCell[];
  genModels: string[];
  judgeModels: string[];
  judgeLabels: Record<string, JudgeLabel>;
  small?: boolean;
  includeTask?: boolean;
}) {
  const map = buildCellMap(cells);
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
              return (
                <td key={j} style={{ background: c.bg, color: c.fg }}>
                  {small ? (
                    Math.round(c.pct! * 100)
                  ) : (
                    <strong>{Math.round(c.pct! * 100)}</strong>
                  )}
                  <span className="n">{c.n}</span>
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
