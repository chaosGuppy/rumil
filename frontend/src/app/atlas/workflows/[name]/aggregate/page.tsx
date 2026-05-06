import Link from "next/link";
import { notFound } from "next/navigation";
import type { WorkflowAggregate, WorkflowProfile, RunRollup } from "@/api";
import { atlasFetch } from "../../../_lib/fetch";
import { Crumbs } from "../../../_components/Crumbs";
import { Sparkline } from "../../../_components/Sparkline";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ name: string }>;
}) {
  const { name } = await params;
  return { title: `${name} · aggregate` };
}

function fmtCost(v: number): string {
  if (v >= 1) return `$${v.toFixed(2)}`;
  if (v >= 0.01) return `$${v.toFixed(3)}`;
  return `$${v.toFixed(4)}`;
}

export default async function WorkflowAggregatePage({
  params,
  searchParams,
}: {
  params: Promise<{ name: string }>;
  searchParams: Promise<{ project_id?: string; limit?: string }>;
}) {
  const { name } = await params;
  const sp = await searchParams;
  const projectId = sp.project_id;
  const limit = sp.limit ?? "50";

  const qs = new URLSearchParams();
  qs.set("limit", limit);
  if (projectId) qs.set("project_id", projectId);

  const [agg, profile] = await Promise.all([
    atlasFetch<WorkflowAggregate | null>(
      `/api/atlas/workflows/${encodeURIComponent(name)}/aggregate?${qs.toString()}`,
      null,
    ),
    atlasFetch<WorkflowProfile | null>(
      `/api/atlas/workflows/${encodeURIComponent(name)}`,
      null,
    ),
  ]);

  if (!agg) notFound();

  const runs: RunRollup[] = agg.runs ?? [];
  const totalCost = runs.reduce((a, r) => a + (r.cost_usd ?? 0), 0);
  const totalDispatches = runs.reduce((a, r) => a + (r.n_dispatches ?? 0), 0);
  const totalCalls = runs.reduce((a, r) => a + (r.n_calls ?? 0), 0);
  const totalPages = runs.reduce((a, r) => a + (r.n_pages_loaded ?? 0), 0);
  const meanPages = runs.length ? totalPages / runs.length : 0;

  const pagesPerRun = (agg.pages_loaded_per_run ?? []).slice().reverse();
  const dispatchesPerRun = (agg.dispatches_per_run ?? []).slice().reverse();
  const costPerRun = (agg.cost_per_run ?? []).slice().reverse();
  const callsPerRun = (agg.calls_per_run ?? []).slice().reverse();
  const sparkLabels = runs
    .slice()
    .reverse()
    .map((r) => r.question_headline ?? r.name ?? "");

  const dispatchFreqs = (agg.dispatch_frequencies ?? []).slice();
  dispatchFreqs.sort((a, b) => b.total - a.total);
  const maxDispatchTotal = dispatchFreqs[0]?.total ?? 1;

  const stageInvocations = agg.stage_invocations ?? [];
  const profileStageOrder = (profile?.stages ?? []).map((s) => s.id);
  const stageMap = new Map(stageInvocations.map((s) => [s.stage_id, s]));
  const orderedStages = profileStageOrder
    .map((id) => stageMap.get(id))
    .filter((s): s is NonNullable<typeof s> => !!s);
  const extraStages = stageInvocations.filter(
    (s) => !profileStageOrder.includes(s.stage_id),
  );
  const allStages = [...orderedStages, ...extraStages];

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs
            items={[
              { label: "atlas", href: "/atlas" },
              { label: "workflows", href: "/atlas/workflows" },
              { label: name, href: `/atlas/workflows/${name}` },
              { label: "aggregate" },
            ]}
          />
          <h1>{name} · aggregate</h1>
          <p className="atlas-lede">
            How <span className="atlas-mono">{name}</span> actually behaves on
            real runs — branches taken vs skipped, dispatch frequency, and
            per-run sparklines for cost / pages-loaded / dispatches / calls.
          </p>
        </div>
      </div>

      {agg.n_runs === 0 ? (
        <div className="atlas-empty">
          <strong>spec only — no runs yet</strong>
          aggregate populates as runs land. Until then, see the{" "}
          <Link href={`/atlas/workflows/${name}`}>stage diagram</Link> for the
          declared workflow.
        </div>
      ) : (
        <>
          <div className="atlas-stat-grid">
            <div className="atlas-stat">
              <span className="atlas-stat-num">{agg.n_runs}</span>
              <span className="atlas-stat-label">runs</span>
            </div>
            <div className="atlas-stat">
              <span className="atlas-stat-num">{totalCalls}</span>
              <span className="atlas-stat-label">calls</span>
            </div>
            <div className="atlas-stat">
              <span className="atlas-stat-num">{totalDispatches}</span>
              <span className="atlas-stat-label">dispatches</span>
            </div>
            <div className="atlas-stat">
              <span className="atlas-stat-num">{fmtCost(totalCost)}</span>
              <span className="atlas-stat-label">total cost</span>
            </div>
            <div className="atlas-stat">
              <span className="atlas-stat-num">{meanPages.toFixed(1)}</span>
              <span className="atlas-stat-label">mean pages loaded</span>
            </div>
          </div>

          <section className="atlas-section">
            <div className="atlas-section-head">
              <h2>per-run sparklines</h2>
              <span className="atlas-section-meta">
                newest right · hover for headline
              </span>
            </div>
            <div className="atlas-spark-row">
              <div className="atlas-spark">
                <div className="atlas-spark-label">pages loaded</div>
                <div className="atlas-spark-value">
                  Σ {pagesPerRun.reduce((a, b) => a + b, 0)}
                </div>
                <div className="atlas-spark-chart">
                  <Sparkline values={pagesPerRun} labels={sparkLabels} />
                </div>
              </div>
              <div className="atlas-spark">
                <div className="atlas-spark-label">dispatches</div>
                <div className="atlas-spark-value">
                  Σ {dispatchesPerRun.reduce((a, b) => a + b, 0)}
                </div>
                <div className="atlas-spark-chart">
                  <Sparkline
                    values={dispatchesPerRun}
                    labels={sparkLabels}
                    color="var(--a-warm)"
                  />
                </div>
              </div>
              <div className="atlas-spark">
                <div className="atlas-spark-label">cost (usd)</div>
                <div className="atlas-spark-value">{fmtCost(totalCost)}</div>
                <div className="atlas-spark-chart">
                  <Sparkline
                    values={costPerRun}
                    labels={sparkLabels}
                    color="var(--a-success)"
                  />
                </div>
              </div>
              <div className="atlas-spark">
                <div className="atlas-spark-label">calls</div>
                <div className="atlas-spark-value">{totalCalls}</div>
                <div className="atlas-spark-chart">
                  <Sparkline
                    values={callsPerRun}
                    labels={sparkLabels}
                    color="var(--a-orchestrator)"
                  />
                </div>
              </div>
            </div>
          </section>

          {allStages.length > 0 && (
            <section className="atlas-section">
              <div className="atlas-section-head">
                <h2>stage invocations</h2>
                <span className="atlas-section-meta">
                  branches taken vs skipped, across {agg.n_runs} runs
                </span>
              </div>
              <div className="atlas-card" style={{ padding: "0.4rem 1.2rem" }}>
                {allStages.map((s) => {
                  const total = s.total_runs || 1;
                  const takenPct = (s.taken_count / total) * 100;
                  const skipPct = (s.skipped_count / total) * 100;
                  return (
                    <div className="atlas-stage-bar" key={s.stage_id}>
                      <div>
                        <div style={{ color: "var(--a-rule)" }}>{s.label}</div>
                        <div style={{ color: "var(--a-muted)", fontSize: "0.66rem" }}>
                          {s.stage_id}
                        </div>
                      </div>
                      <div className="atlas-stage-bar-track">
                        <div
                          className="atlas-stage-bar-fill"
                          style={{ width: `${takenPct}%` }}
                        />
                        <div
                          className="atlas-stage-bar-skip"
                          style={{
                            left: `${takenPct}%`,
                            width: `${skipPct}%`,
                          }}
                        />
                      </div>
                      <div className="atlas-stage-bar-counts">
                        {s.taken_count} / {s.total_runs}
                      </div>
                    </div>
                  );
                })}
                <div
                  style={{
                    fontSize: "0.66rem",
                    color: "var(--a-muted)",
                    fontFamily: "var(--a-mono)",
                    marginTop: "0.6rem",
                    paddingTop: "0.5rem",
                    borderTop: "1px solid var(--a-line)",
                    display: "flex",
                    gap: "1.2rem",
                  }}
                >
                  <span>
                    <span
                      style={{
                        display: "inline-block",
                        width: 10,
                        height: 10,
                        background: "var(--a-success)",
                        marginRight: 6,
                        verticalAlign: "middle",
                      }}
                    />
                    taken
                  </span>
                  <span>
                    <span
                      style={{
                        display: "inline-block",
                        width: 10,
                        height: 10,
                        background: "var(--a-warm)",
                        opacity: 0.55,
                        marginRight: 6,
                        verticalAlign: "middle",
                      }}
                    />
                    skipped
                  </span>
                </div>
              </div>
            </section>
          )}

          {dispatchFreqs.length > 0 && (
            <section className="atlas-section">
              <div className="atlas-section-head">
                <h2>dispatch frequency</h2>
                <span className="atlas-section-meta">
                  by call type · click to inspect
                </span>
              </div>
              <div className="atlas-card" style={{ padding: "0.7rem 1.2rem" }}>
                {dispatchFreqs.map((d) => (
                  <Link
                    key={d.call_type}
                    href={`/atlas/calls/${encodeURIComponent(d.call_type)}`}
                    className="atlas-disp-bar"
                  >
                    <span className="atlas-disp-bar-name">{d.call_type}</span>
                    <span className="atlas-disp-bar-track">
                      <span
                        className="atlas-disp-bar-fill"
                        style={{
                          width: `${(d.total / maxDispatchTotal) * 100}%`,
                        }}
                      />
                    </span>
                    <span className="atlas-disp-bar-count">
                      {d.total} · {d.avg_per_run.toFixed(1)}/run
                    </span>
                  </Link>
                ))}
              </div>
            </section>
          )}

          <section className="atlas-section">
            <div className="atlas-section-head">
              <h2>runs</h2>
              <span className="atlas-section-meta">
                {runs.length} most recent · sortable list with outcomes lives at{" "}
                <Link
                  href={`/atlas/workflows/${encodeURIComponent(name)}/runs${projectId ? `?project_id=${projectId}` : ""}`}
                >
                  /runs
                </Link>
              </span>
            </div>
            <div style={{ display: "flex", gap: "0.6rem", flexWrap: "wrap" }}>
              <Link
                href={`/atlas/workflows/${encodeURIComponent(name)}/runs${projectId ? `?project_id=${projectId}` : ""}`}
                className="atlas-toggle-link is-active"
              >
                see all runs →
              </Link>
              <Link
                href={`/atlas/workflows/${encodeURIComponent(name)}/runs?order_by=cost${projectId ? `&project_id=${projectId}` : ""}`}
                className="atlas-toggle-link"
              >
                sort by cost
              </Link>
              <Link
                href={`/atlas/workflows/${encodeURIComponent(name)}/runs?order_by=duration${projectId ? `&project_id=${projectId}` : ""}`}
                className="atlas-toggle-link"
              >
                sort by duration
              </Link>
              <Link
                href={`/atlas/workflows/${encodeURIComponent(name)}/runs?include_noop=false${projectId ? `&project_id=${projectId}` : ""}`}
                className="atlas-toggle-link"
              >
                hide noops
              </Link>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
