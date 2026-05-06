import Link from "next/link";
import type { CallTypeStats } from "@/api";

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

export function CallStatsPanel({ stats }: { stats: CallTypeStats }) {
  const total = stats.n_invocations ?? 0;
  const statuses = stats.status_counts ?? {};
  const statusEntries = Object.entries(statuses).sort((a, b) => b[1] - a[1]);
  const statusTotal = statusEntries.reduce((a, [, n]) => a + n, 0) || 1;
  const topMoves = stats.top_moves ?? [];
  const topCo = stats.top_co_firings ?? [];
  const errors = stats.recent_errors ?? [];

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
                {errors.slice(0, 4).map((e, i) => (
                  <li key={i}>
                    {e.length > 280 ? e.slice(0, 277) + "…" : e}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
