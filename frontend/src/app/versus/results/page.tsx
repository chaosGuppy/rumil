import Link from "next/link";
import type { Metadata } from "next";
import type { ResultsBundle } from "@/api/types.gen";
import { API_BASE, serverFetch } from "@/lib/api-base";
import { AutoSubmitSelect } from "@/components/versus/AutoSubmitSelect";
import { MatrixTable } from "@/components/versus/MatrixTable";
import { VersusHeader } from "@/components/versus/VersusHeader";
import "../versus.css";

export const metadata: Metadata = { title: "versus · results" };

async function getResults(
  criterion: string | undefined,
  includeContaminated: boolean,
): Promise<ResultsBundle | null> {
  const qs = new URLSearchParams();
  if (criterion) qs.set("criterion", criterion);
  if (includeContaminated) qs.set("include_contaminated", "true");
  const res = await serverFetch(`${API_BASE}/api/versus/results?${qs}`, {
    cache: "no-store",
  });
  if (!res.ok) return null;
  return res.json();
}

function deltaClass(pct: number): string {
  if (pct < -20 || pct > 20) return "delta-bad";
  if (pct < -5 || pct > 5) return "delta-warn";
  return "delta-good";
}

export default async function VersusResultsPage({
  searchParams,
}: {
  searchParams: Promise<{ criterion?: string; include_contaminated?: string }>;
}) {
  const sp = await searchParams;
  const criterion = sp.criterion;
  const includeContaminated = sp.include_contaminated === "true";
  const data = await getResults(criterion, includeContaminated);

  if (!data) {
    return (
      <div className="versus-shell">
        <VersusHeader breadcrumb="results" />
        <main className="versus-main">
          <div className="versus-card">
            <em className="versus-muted">
              Failed to load results. Make sure the API is running and versus config exists.
            </em>
          </div>
        </main>
      </div>
    );
  }

  const {
    criteria,
    active_criterion,
    gen_models,
    judge_models,
    judge_labels,
    main_matrices,
    small_grid,
    rows,
    total_judgments,
    total_completions,
    sources_summary,
  } = data;

  const empty = gen_models.length === 0 || judge_models.length === 0;

  return (
    <div className="versus-shell">
      <VersusHeader
        breadcrumb="results"
        right={
          <span className="muted">
            {total_completions} completions · {total_judgments} judgments
          </span>
        }
      />
      <main className="versus-main">
        <div className="results-topbar">
          <h2 title="Higher = judge preferred the human continuation. 50% is neutral. Ties count as ½. Cell: pct% (n).">
            %-picks-human · gen × judge
          </h2>
          <form method="get" action="/versus/results">
            <AutoSubmitSelect
              name="criterion"
              defaultValue={active_criterion ?? ""}
              className="versus-select"
              style={{ padding: "4px 8px", fontSize: 13 }}
              options={[
                { value: "", label: "avg across criteria" },
                ...criteria.map((c) => ({ value: c, label: c })),
              ]}
            />
            <noscript>
              <button type="submit" className="versus-button">apply</button>
            </noscript>
          </form>
        </div>

        {empty ? (
          <div className="versus-card">
            <em className="versus-muted">No judgments yet.</em>
          </div>
        ) : (
          <div className="main-matrices">
            {main_matrices.map((mm) => (
              <section key={mm.condition}>
                <div className="cond-head">
                  <span className="versus-pill">{mm.condition}</span>
                  <span className="cond-title">{mm.meta.title}</span>
                </div>
                <div className="cond-desc">{mm.meta.pair}</div>
                <div className="cond-desc">{mm.meta.cell_meaning}</div>
                <MatrixTable
                  cells={mm.cells}
                  genModels={gen_models}
                  judgeModels={judge_models}
                  judgeLabels={judge_labels}
                />
              </section>
            ))}
          </div>
        )}

        <h3 className="results-section-head">Faceted by criterion × condition</h3>
        <p className="versus-muted" style={{ fontSize: 12, margin: "-4px 0 8px" }}>
          Rows: condition. Cols: criterion. Cell values follow the same semantic as the main matrix
          above for that condition.
        </p>
        <div
          className="facet-grid"
          style={{ gridTemplateColumns: `auto repeat(${criteria.length}, 1fr)` }}
        >
          <div></div>
          {criteria.map((c) => (
            <div key={c} className="facet-col-head">
              <span className="versus-pill">{c}</span>
            </div>
          ))}
          {small_grid.map((block) => (
            <BlockRow
              key={block.condition}
              block={block}
              empty={empty}
              genModels={gen_models}
              judgeModels={judge_models}
              judgeLabels={judge_labels}
            />
          ))}
        </div>

        {sources_summary.length > 0 && (
          <details className="collapsible">
            <summary>Source length sanity</summary>
            <table className="log" style={{ maxWidth: 640, marginTop: 6 }}>
              <thead>
                <tr>
                  <th>source</th>
                  <th>n</th>
                  <th>avg words</th>
                  <th>Δ vs target</th>
                </tr>
              </thead>
              <tbody>
                {sources_summary.map((s) => (
                  <tr key={s.source_id}>
                    <td className="versus-mono">{s.source_id}</td>
                    <td>{s.n}</td>
                    <td>{s.avg_words}</td>
                    <td className={deltaClass(s.avg_delta_pct)}>
                      {s.avg_delta_pct >= 0 ? "+" : ""}
                      {s.avg_delta_pct.toFixed(1)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        )}

        <details className="collapsible">
          <summary>All judgments ({rows.length})</summary>
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
                  <th>ts</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={i}>
                    <td className="versus-mono">
                      <Link href={`/versus/inspect?essay=${encodeURIComponent(r.essay_id)}`}>
                        {r.essay_id}
                      </Link>
                    </td>
                    <td className="versus-mono">{r.source_a}</td>
                    <td className="versus-mono">{r.source_b}</td>
                    <td>{r.criterion}</td>
                    <td className="versus-mono">{r.judge_model}</td>
                    <td>{r.verdict}</td>
                    <td className="versus-mono">{r.winner}</td>
                    <td className="versus-muted">{r.ts}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      </main>
    </div>
  );
}

function BlockRow({
  block,
  empty,
  genModels,
  judgeModels,
  judgeLabels,
}: {
  block: ResultsBundle["small_grid"][number];
  empty: boolean;
  genModels: string[];
  judgeModels: string[];
  judgeLabels: ResultsBundle["judge_labels"];
}) {
  return (
    <>
      <div className="facet-row-head">
        <span className="versus-pill subtle">{block.condition}</span>
      </div>
      {block.per_crit.map((cellBlock) => (
        <div key={cellBlock.criterion} className="facet-card">
          {empty ? (
            <em className="versus-muted">no data</em>
          ) : (
            <MatrixTable
              cells={cellBlock.cells}
              genModels={genModels}
              judgeModels={judgeModels}
              judgeLabels={judgeLabels}
              small
              includeTask={false}
            />
          )}
        </div>
      ))}
    </>
  );
}
