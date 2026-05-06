import Link from "next/link";
import type { CallTypeStats } from "@/api";
import { Histogram } from "./Histogram";
import { Sparkline } from "./Sparkline";

function fmtCost(v: number | undefined | null): string {
  if (v == null) return "—";
  if (v >= 1) return `$${v.toFixed(2)}`;
  if (v >= 0.01) return `$${v.toFixed(3)}`;
  return `$${v.toFixed(4)}`;
}

function fmtNum(v: number | undefined | null, digits = 1): string {
  if (v == null) return "—";
  return v.toFixed(digits);
}

const BUCKETS = ["off", "day", "week", "month"] as const;
type Bucket = (typeof BUCKETS)[number];

export function CallStatsPanel({
  stats,
  callType,
  bucket,
}: {
  stats: CallTypeStats;
  callType?: string;
  bucket?: string | null;
}) {
  const total = stats.n_invocations ?? 0;
  const statuses = stats.status_counts ?? {};
  const statusEntries = Object.entries(statuses).sort((a, b) => b[1] - a[1]);
  const statusTotal = statusEntries.reduce((a, [, n]) => a + n, 0) || 1;
  const topMoves = stats.top_moves ?? [];
  const topCo = stats.top_co_firings ?? [];
  const errors = stats.recent_errors ?? [];
  const roundsHisto = stats.rounds_histogram ?? [];
  const costHisto = stats.cost_histogram ?? [];
  const pagesHisto = stats.pages_loaded_histogram ?? [];
  const series = stats.series ?? [];
  const activeBucket: Bucket = (BUCKETS as readonly string[]).includes(
    bucket ?? "",
  )
    ? (bucket as Bucket)
    : "off";
  const hasPercentiles =
    stats.p50_cost_usd != null ||
    stats.p90_cost_usd != null ||
    stats.p99_cost_usd != null;

  return (
    <section className="atlas-section">
      <div className="atlas-section-head">
        <h2>empirical stats</h2>
        <span className="atlas-section-meta">
          observed across {stats.scanned_runs ?? 0} recent run
          {stats.scanned_runs === 1 ? "" : "s"}
          {(stats.runs_with_call ?? 0) > 0 && (
            <>
              <span className="atlas-sep">·</span>
              {stats.runs_with_call} contained this call
            </>
          )}
        </span>
      </div>

      {total === 0 ? (
        <div className="atlas-empty">
          <strong>no invocations yet</strong>
          stats populate as this call type fires.
        </div>
      ) : (
        <div className="atlas-stat-panel">
          <div className="atlas-stat-mini-grid">
            <div className="atlas-stat-mini">
              <div className="atlas-stat-mini-num">{total}</div>
              <div className="atlas-stat-mini-label">invocations</div>
            </div>
            <div className="atlas-stat-mini">
              <div className="atlas-stat-mini-num">{fmtCost(stats.mean_cost_usd)}</div>
              <div className="atlas-stat-mini-label">mean cost</div>
            </div>
            <div className="atlas-stat-mini">
              <div className="atlas-stat-mini-num">{fmtCost(stats.total_cost_usd)}</div>
              <div className="atlas-stat-mini-label">total cost</div>
            </div>
            <div className="atlas-stat-mini">
              <div className="atlas-stat-mini-num">{fmtNum(stats.mean_rounds, 2)}</div>
              <div className="atlas-stat-mini-label">mean rounds</div>
            </div>
            <div className="atlas-stat-mini">
              <div className="atlas-stat-mini-num">{fmtNum(stats.mean_pages_loaded, 1)}</div>
              <div className="atlas-stat-mini-label">mean pages loaded</div>
            </div>
          </div>

          {statusEntries.length > 0 && (
            <div style={{ marginBottom: "1rem" }}>
              <div className="atlas-stat-panel-meta" style={{ marginBottom: "0.3rem" }}>
                status mix
              </div>
              <div className="atlas-status-bar" role="img" aria-label="status mix">
                {statusEntries.map(([s, n]) => {
                  const pct = (n / statusTotal) * 100;
                  const cls =
                    s === "complete"
                      ? "is-complete"
                      : s === "error"
                        ? "is-error"
                        : "is-other";
                  return (
                    <div
                      key={s}
                      className={`atlas-status-bar-seg ${cls}`}
                      style={{ width: `${pct}%` }}
                      title={`${s}: ${n}`}
                    />
                  );
                })}
              </div>
              <div className="atlas-status-bar-legend">
                {statusEntries.map(([s, n]) => (
                  <span key={s}>
                    {s} · {n}
                  </span>
                ))}
              </div>
            </div>
          )}

          {hasPercentiles && (
            <div className="atlas-percentile-row">
              <div className="atlas-percentile">
                <span className="atlas-percentile-label">p50 cost</span>
                <span className="atlas-percentile-value">{fmtCost(stats.p50_cost_usd)}</span>
              </div>
              <div className="atlas-percentile">
                <span className="atlas-percentile-label">p90 cost</span>
                <span className="atlas-percentile-value">{fmtCost(stats.p90_cost_usd)}</span>
              </div>
              <div className="atlas-percentile">
                <span className="atlas-percentile-label">p99 cost</span>
                <span className="atlas-percentile-value">{fmtCost(stats.p99_cost_usd)}</span>
              </div>
            </div>
          )}

          {(roundsHisto.length > 0 || costHisto.length > 0 || pagesHisto.length > 0) && (
            <div className="atlas-histo-grid">
              {roundsHisto.length > 0 && (
                <div className="atlas-histo">
                  <div className="atlas-histo-head">
                    <span className="atlas-histo-title">rounds</span>
                    <span className="atlas-histo-meta">{roundsHisto.length} bins</span>
                  </div>
                  <Histogram bins={roundsHisto} color="var(--a-orchestrator)" />
                </div>
              )}
              {costHisto.length > 0 && (
                <div className="atlas-histo">
                  <div className="atlas-histo-head">
                    <span className="atlas-histo-title">cost (usd)</span>
                    <span className="atlas-histo-meta">{costHisto.length} bins</span>
                  </div>
                  <Histogram bins={costHisto} color="var(--a-success)" />
                </div>
              )}
              {pagesHisto.length > 0 && (
                <div className="atlas-histo">
                  <div className="atlas-histo-head">
                    <span className="atlas-histo-title">pages loaded</span>
                    <span className="atlas-histo-meta">{pagesHisto.length} bins</span>
                  </div>
                  <Histogram bins={pagesHisto} color="var(--a-accent)" />
                </div>
              )}
            </div>
          )}

          {callType && (
            <div style={{ marginBottom: "1rem" }}>
              <div
                className="atlas-stat-panel-meta"
                style={{ marginBottom: "0.4rem", display: "flex", alignItems: "center", gap: "0.4rem", flexWrap: "wrap" }}
              >
                <span>time series</span>
                <div className="atlas-bucket-tabs" role="tablist">
                  {BUCKETS.map((bk) => (
                    <Link
                      key={bk}
                      role="tab"
                      aria-selected={activeBucket === bk}
                      href={
                        bk === "off"
                          ? `/atlas/calls/${encodeURIComponent(callType)}`
                          : `/atlas/calls/${encodeURIComponent(callType)}?bucket=${bk}`
                      }
                      className={`atlas-bucket-tab ${activeBucket === bk ? "is-active" : ""}`}
                    >
                      {bk}
                    </Link>
                  ))}
                </div>
                {series.length > 0 && (
                  <span style={{ marginLeft: "auto" }}>
                    {series.length} bucket{series.length === 1 ? "" : "s"}
                  </span>
                )}
              </div>
              {activeBucket !== "off" && series.length > 0 && (
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(auto-fit, minmax(14rem, 1fr))",
                    gap: "1rem",
                    marginTop: "0.4rem",
                  }}
                >
                  <div className="atlas-spark">
                    <div className="atlas-spark-label">invocations / {activeBucket}</div>
                    <div className="atlas-spark-value">
                      Σ {series.reduce((a, b) => a + (b.n_invocations ?? 0), 0)}
                    </div>
                    <div className="atlas-spark-chart">
                      <Sparkline
                        values={series.map((s) => s.n_invocations ?? 0)}
                        labels={series.map((s) => s.bucket_start.slice(0, 10))}
                        color="var(--a-accent)"
                      />
                    </div>
                  </div>
                  <div className="atlas-spark">
                    <div className="atlas-spark-label">total cost / {activeBucket}</div>
                    <div className="atlas-spark-value">
                      {fmtCost(series.reduce((a, b) => a + (b.total_cost_usd ?? 0), 0))}
                    </div>
                    <div className="atlas-spark-chart">
                      <Sparkline
                        values={series.map((s) => s.total_cost_usd ?? 0)}
                        labels={series.map((s) => s.bucket_start.slice(0, 10))}
                        color="var(--a-success)"
                      />
                    </div>
                  </div>
                  <div className="atlas-spark">
                    <div className="atlas-spark-label">mean cost / {activeBucket}</div>
                    <div className="atlas-spark-value">
                      {fmtCost(
                        series.reduce((a, b) => a + (b.mean_cost_usd ?? 0), 0) /
                          (series.length || 1),
                      )}
                    </div>
                    <div className="atlas-spark-chart">
                      <Sparkline
                        values={series.map((s) => s.mean_cost_usd ?? 0)}
                        labels={series.map((s) => s.bucket_start.slice(0, 10))}
                        color="var(--a-warm)"
                      />
                    </div>
                  </div>
                </div>
              )}
              {activeBucket !== "off" && series.length === 0 && (
                <div className="atlas-empty" style={{ padding: "1rem", marginTop: "0.4rem" }}>
                  no series data yet for bucket = {activeBucket}
                </div>
              )}
            </div>
          )}

          {topMoves.length > 0 && (
            <div style={{ marginBottom: "1rem" }}>
              <div className="atlas-stat-panel-meta" style={{ marginBottom: "0.4rem" }}>
                top moves fired
              </div>
              <div className="atlas-chip-row">
                {topMoves.map((m) => (
                  <Link
                    key={m.move_type}
                    href={`/atlas/moves/${encodeURIComponent(m.move_type)}`}
                    className="atlas-chip is-accent"
                  >
                    {m.move_type}
                    <span style={{ color: "var(--a-muted)", marginLeft: "0.3rem" }}>
                      · {m.count}
                    </span>
                  </Link>
                ))}
              </div>
            </div>
          )}

          {topCo.length > 0 && (
            <div style={{ marginBottom: "1rem" }}>
              <div className="atlas-stat-panel-meta" style={{ marginBottom: "0.4rem" }}>
                often co-fires with
              </div>
              <div className="atlas-chip-row">
                {topCo.map((c) => (
                  <span key={`${c.a}-${c.b}`} className="atlas-chip">
                    <Link
                      href={`/atlas/moves/${encodeURIComponent(c.a)}`}
                      style={{ color: "inherit" }}
                    >
                      {c.a}
                    </Link>
                    <span style={{ color: "var(--a-muted)", margin: "0 0.2rem" }}>+</span>
                    <Link
                      href={`/atlas/moves/${encodeURIComponent(c.b)}`}
                      style={{ color: "inherit" }}
                    >
                      {c.b}
                    </Link>
                    <span style={{ color: "var(--a-muted)", marginLeft: "0.3rem" }}>
                      · {c.count}
                    </span>
                  </span>
                ))}
              </div>
            </div>
          )}

          {errors.length > 0 && (
            <div>
              <div className="atlas-stat-panel-meta" style={{ marginBottom: "0.4rem" }}>
                recent errors ({errors.length})
              </div>
              <ul className="atlas-error-list">
                {errors.slice(0, 4).map((e, i) => {
                  const msg = e.message ?? "";
                  const trimmed = msg.length > 280 ? msg.slice(0, 277) + "…" : msg;
                  return (
                    <li key={i}>
                      <div>{trimmed}</div>
                      <div className="atlas-error-refs">
                        {e.exchange_id && (
                          <Link
                            href={`/atlas/exchanges/${encodeURIComponent(
                              e.exchange_id,
                            )}/playground`}
                            title="open this exchange in the playground"
                          >
                            exchange {e.exchange_id.slice(0, 8)} →
                          </Link>
                        )}
                        {e.call_id && (
                          <Link
                            href={`/atlas/calls/by_id/${encodeURIComponent(
                              e.call_id,
                            )}/exchanges`}
                            title="every exchange recorded against this call"
                          >
                            call {e.call_id.slice(0, 8)} →
                          </Link>
                        )}
                        {e.run_id && (
                          <Link
                            href={`/atlas/runs/${encodeURIComponent(e.run_id)}/flow`}
                            title="run flow"
                          >
                            run {e.run_id.slice(0, 8)} →
                          </Link>
                        )}
                        <span className="atlas-error-source">{e.source}</span>
                      </div>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
