import Link from "next/link";
import type { MoveStats } from "@/api";

function fmtDate(s: string | null | undefined): string {
  if (!s) return "—";
  try {
    const d = new Date(s);
    return d.toISOString().slice(0, 10);
  } catch {
    return s;
  }
}

export function MoveStatsPanel({ stats }: { stats: MoveStats }) {
  const total = stats.n_invocations ?? 0;
  const byCallType = stats.invocations_by_call_type ?? [];
  const max = byCallType.reduce((m, x) => Math.max(m, x.count), 0) || 1;

  return (
    <section className="atlas-section">
      <div className="atlas-section-head">
        <h2>empirical stats</h2>
        <span className="atlas-section-meta">
          observed across {stats.scanned_runs ?? 0} recent run
          {stats.scanned_runs === 1 ? "" : "s"}
          {(stats.runs_with_move ?? 0) > 0 && (
            <>
              <span className="atlas-sep">·</span>
              {stats.runs_with_move} contained this move
            </>
          )}
        </span>
      </div>

      {total === 0 ? (
        <div className="atlas-empty">
          <strong>no invocations yet</strong>
          stats populate as this move fires.
        </div>
      ) : (
        <div className="atlas-stat-panel">
          <div className="atlas-stat-mini-grid">
            <div className="atlas-stat-mini">
              <div className="atlas-stat-mini-num">{total}</div>
              <div className="atlas-stat-mini-label">invocations</div>
            </div>
            <div className="atlas-stat-mini">
              <div className="atlas-stat-mini-num">{stats.runs_with_move ?? 0}</div>
              <div className="atlas-stat-mini-label">runs with move</div>
            </div>
            <div className="atlas-stat-mini">
              <div className="atlas-stat-mini-num" style={{ fontSize: "0.85rem" }}>
                {fmtDate(stats.last_seen)}
              </div>
              <div className="atlas-stat-mini-label">last seen</div>
            </div>
          </div>

          {(stats.created_pages_n ?? 0) > 0 && (
            <div style={{ margin: "1rem 0" }}>
              <div
                className="atlas-stat-panel-meta"
                style={{ marginBottom: "0.4rem", display: "flex", gap: "0.4rem", alignItems: "baseline" }}
              >
                <span>page survival</span>
                <span style={{ color: "var(--a-muted)", fontWeight: 400 }}>
                  · of pages this move created in scanned runs, share still alive (not superseded)
                </span>
              </div>
              <div className="atlas-stat-mini-grid">
                <div className="atlas-stat-mini">
                  <div
                    className="atlas-stat-mini-num"
                    style={{
                      color:
                        (stats.survival_pct ?? 0) >= 70
                          ? "var(--a-success)"
                          : (stats.survival_pct ?? 0) <= 40
                          ? "var(--a-warm)"
                          : undefined,
                    }}
                  >
                    {(stats.survival_pct ?? 0).toFixed(1)}%
                  </div>
                  <div className="atlas-stat-mini-label" title="survived / created_pages_n">
                    survival rate
                  </div>
                </div>
                <div className="atlas-stat-mini">
                  <div className="atlas-stat-mini-num">{stats.survived_n ?? 0}</div>
                  <div className="atlas-stat-mini-label">still alive</div>
                </div>
                <div className="atlas-stat-mini">
                  <div className="atlas-stat-mini-num">{stats.superseded_n ?? 0}</div>
                  <div className="atlas-stat-mini-label">superseded</div>
                </div>
                <div className="atlas-stat-mini">
                  <div className="atlas-stat-mini-num">{stats.created_pages_n ?? 0}</div>
                  <div className="atlas-stat-mini-label">created pages tracked</div>
                </div>
              </div>
            </div>
          )}

          {byCallType.length > 0 && (
            <div>
              <div className="atlas-stat-panel-meta" style={{ marginBottom: "0.4rem" }}>
                fires from these call types
              </div>
              <div>
                {byCallType.map((b) => {
                  const pct = (b.count / max) * 100;
                  return (
                    <Link
                      key={b.call_type}
                      href={`/atlas/calls/${encodeURIComponent(b.call_type)}`}
                      className="atlas-disp-bar"
                    >
                      <span className="atlas-disp-bar-name">{b.call_type}</span>
                      <span className="atlas-disp-bar-track">
                        <span className="atlas-disp-bar-fill" style={{ width: `${pct}%` }} />
                      </span>
                      <span className="atlas-disp-bar-count">{b.count}</span>
                    </Link>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
