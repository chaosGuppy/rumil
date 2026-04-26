import type { Metadata } from "next";
import { Fragment } from "react";
import type { ResultsBundle } from "@/api/types.gen";
import { API_BASE, serverFetch } from "@/lib/api-base";
import { AutoSubmitCheckbox } from "@/components/versus/AutoSubmitCheckbox";
import { AutoSubmitSelect } from "@/components/versus/AutoSubmitSelect";
import { DiagnosticsPane, fetchDiagnostics } from "@/components/versus/DiagnosticsPane";
import { JudgmentRowsTable } from "@/components/versus/JudgmentRowsTable";
import { MatrixTable } from "@/components/versus/MatrixTable";
import { VersusHeader } from "@/components/versus/VersusHeader";
import "../versus.css";

export const metadata: Metadata = { title: "versus · results" };

async function getResults(
  criterion: string | undefined,
  includeContaminated: boolean,
  includeStale: boolean,
  rowFilter: {
    gen?: string;
    judge?: string;
    condition?: string;
    criterion?: string;
  },
): Promise<ResultsBundle | null> {
  const qs = new URLSearchParams();
  if (criterion) qs.set("criterion", criterion);
  if (includeContaminated) qs.set("include_contaminated", "true");
  qs.set("include_stale", includeStale ? "true" : "false");
  if (rowFilter.gen) qs.set("filter_gen", rowFilter.gen);
  if (rowFilter.judge) qs.set("filter_judge", rowFilter.judge);
  if (rowFilter.condition) qs.set("filter_condition", rowFilter.condition);
  if (rowFilter.criterion) qs.set("filter_criterion", rowFilter.criterion);
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
  searchParams: Promise<{
    criterion?: string;
    include_contaminated?: string;
    include_stale?: string;
    filter_gen?: string;
    filter_judge?: string;
    filter_condition?: string;
    filter_criterion?: string;
  }>;
}) {
  const sp = await searchParams;
  const criterion = sp.criterion;
  const includeContaminated = sp.include_contaminated === "true";
  // Default include_stale=false so the matrices reflect only judgments
  // against current essay text. Pass ?include_stale=true to mix in
  // historical rows tied to older prefix_config_hashes.
  const includeStale = sp.include_stale === "true";
  const rowFilter = {
    gen: sp.filter_gen,
    judge: sp.filter_judge,
    condition: sp.filter_condition,
    criterion: sp.filter_criterion,
  };
  const [data, diagnostics] = await Promise.all([
    getResults(criterion, includeContaminated, includeStale, rowFilter),
    fetchDiagnostics(criterion, includeContaminated, includeStale),
  ]);

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
    completion_per_source,
    small_grid,
    rows,
    total_judgments,
    total_completions,
    sources_summary,
    essays_status,
    stale_count,
    current_count,
    include_stale,
    row_filter,
    rows_total_before_filter,
  } = data;

  const hasRowFilter =
    Boolean(row_filter.gen) ||
    Boolean(row_filter.judge) ||
    Boolean(row_filter.condition) ||
    Boolean(row_filter.criterion);
  // Preserve the main page controls (criterion, include_stale,
  // include_contaminated) when clearing just the cell-drill-in filter.
  const clearFilterHref = (() => {
    const qs = new URLSearchParams();
    if (active_criterion) qs.set("criterion", active_criterion);
    qs.set("include_stale", includeStale ? "true" : "false");
    if (includeContaminated) qs.set("include_contaminated", "true");
    const s = qs.toString();
    return `/versus/results${s ? `?${s}` : ""}#judgments`;
  })();

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
        {stale_count > 0 && (
          <div className={`stale-banner${include_stale ? "" : " excluded"}`}>
            <strong>{stale_count}</strong>{" "}
            of {stale_count + current_count} judgments reference an older essay
            version (re-import drift). Aggregates below{" "}
            {include_stale ? "include" : "exclude"} them. See the{" "}
            <a href="#essays-status">essays panel</a> for per-essay state.
          </div>
        )}
        <div className="results-topbar">
          <h2 title="Higher = judge preferred the human continuation. 50% is neutral. Ties count as ½. Cell: pct% (n).">
            %-picks-human · gen × judge
          </h2>
          <form method="get" action="/versus/results" className="results-controls">
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
            <AutoSubmitCheckbox
              name="include_stale"
              value="true"
              defaultChecked={include_stale}
              label="include stale"
            />
            <AutoSubmitCheckbox
              name="include_contaminated"
              value="true"
              defaultChecked={includeContaminated}
              label="include contaminated"
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
              <Fragment key={mm.condition}>
                <section>
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
                    condition={mm.condition}
                    criterion={active_criterion}
                    includeStale={includeStale}
                    includeContaminated={includeContaminated}
                  />
                </section>
                {mm.condition === "completion" && completion_per_source.length > 0 && (
                  <div className="per-source-matrices">
                    <div className="per-source-head">by essay source</div>
                    {completion_per_source.map((sm) => (
                      <section key={sm.source_id}>
                        <div className="cond-head">
                          <span className="versus-pill subtle">{sm.source_id}</span>
                          <span className="cond-title">{sm.matrix.meta.title}</span>
                        </div>
                        <MatrixTable
                          cells={sm.matrix.cells}
                          genModels={gen_models}
                          judgeModels={judge_models}
                          judgeLabels={judge_labels}
                          condition={sm.matrix.condition}
                          criterion={active_criterion}
                          includeStale={includeStale}
                          includeContaminated={includeContaminated}
                        />
                      </section>
                    ))}
                  </div>
                )}
              </Fragment>
            ))}
          </div>
        )}

        {criteria.length > 1 && (
          <>
            <h3 className="results-section-head">Faceted by criterion × condition</h3>
            <p className="versus-muted" style={{ fontSize: 12, margin: "-4px 0 8px" }}>
              Rows: condition. Cols: criterion. Cell values follow the same semantic as the main
              matrix above for that condition.
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
                  includeStale={includeStale}
                  includeContaminated={includeContaminated}
                />
              ))}
            </div>
          </>
        )}

        {sources_summary.length > 0 && (
          <details className="collapsible">
            <summary>Source length sanity</summary>
            <div>
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
            </div>
          </details>
        )}

        {essays_status.length > 0 && (
          <details className="collapsible" id="essays-status" open>
            <summary>Essays ({essays_status.length})</summary>
            <div>
              <table className="log" style={{ marginTop: 6 }}>
                <thead>
                  <tr>
                    <th>essay</th>
                    <th>schema</th>
                    <th>current prefix_hash</th>
                    <th>validator</th>
                    <th>issues</th>
                    <th>model</th>
                  </tr>
                </thead>
                <tbody>
                  {essays_status.map((e) => (
                    <tr key={e.essay_id}>
                      <td className="versus-mono" title={e.title}>{e.essay_id}</td>
                      <td>{e.schema_version}</td>
                      <td className="versus-mono">{e.current_prefix_hash}</td>
                      <td>
                        {e.validator_clean === null ? (
                          <span className="versus-muted">no verdict</span>
                        ) : e.validator_clean ? (
                          <span className="versus-pill clean">clean</span>
                        ) : (
                          <span className="versus-pill stale">issues</span>
                        )}
                      </td>
                      <td>{e.validator_issues || ""}</td>
                      <td className="versus-mono versus-muted">{e.validator_model ?? ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        )}

        <DiagnosticsPane data={diagnostics} />

        <details className="collapsible" id="judgments" open={hasRowFilter}>
          <summary>
            All judgments ({rows.length}
            {hasRowFilter ? ` of ${rows_total_before_filter}` : ""})
          </summary>
          {hasRowFilter && (
            <div className="row-filter-banner" role="status">
              <span className="row-filter-label">filtered to</span>
              {row_filter.condition && (
                <>
                  <code>{row_filter.condition}</code>
                </>
              )}
              {row_filter.gen && (
                <>
                  <span className="versus-muted">gen</span>
                  <code>{row_filter.gen}</code>
                </>
              )}
              {row_filter.judge && (
                <>
                  <span className="versus-muted">judge</span>
                  <code>{row_filter.judge}</code>
                </>
              )}
              {row_filter.criterion && (
                <>
                  <span className="versus-muted">criterion</span>
                  <code>{row_filter.criterion}</code>
                </>
              )}
              <a className="row-filter-clear" href={clearFilterHref}>
                clear filter
              </a>
            </div>
          )}
          <JudgmentRowsTable rows={rows} />
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
  includeStale,
  includeContaminated,
}: {
  block: ResultsBundle["small_grid"][number];
  empty: boolean;
  genModels: string[];
  judgeModels: string[];
  judgeLabels: ResultsBundle["judge_labels"];
  includeStale: boolean;
  includeContaminated: boolean;
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
              condition={block.condition}
              criterion={cellBlock.criterion}
              includeStale={includeStale}
              includeContaminated={includeContaminated}
            />
          )}
        </div>
      ))}
    </>
  );
}
