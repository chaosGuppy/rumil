import type { DiagnosticsBundle } from "@/api/types.gen";
import { API_BASE, serverFetch } from "@/lib/api-base";

export async function fetchDiagnostics(
  criterion: string | undefined,
  includeContaminated: boolean,
  includeStale: boolean,
): Promise<DiagnosticsBundle | null> {
  const qs = new URLSearchParams();
  if (criterion) qs.set("criterion", criterion);
  if (includeContaminated) qs.set("include_contaminated", "true");
  qs.set("include_stale", includeStale ? "true" : "false");
  const res = await serverFetch(`${API_BASE}/api/versus/diagnostics?${qs}`, {
    cache: "no-store",
  });
  if (!res.ok) return null;
  return res.json();
}

function pctBand(pct: number): "good" | "warn" | "bad" {
  const d = Math.abs(pct - 50);
  if (d <= 5) return "good";
  if (d <= 10) return "warn";
  return "bad";
}

function fmtPct(pct: number | null | undefined, digits = 1): string {
  if (pct === null || pct === undefined) return "—";
  return `${pct.toFixed(digits)}%`;
}

function fmtCI(lo: number | null | undefined, hi: number | null | undefined): string {
  if (lo === null || lo === undefined || hi === null || hi === undefined) return "";
  return `[${lo.toFixed(0)}, ${hi.toFixed(0)}]`;
}

export function DiagnosticsPane({ data }: { data: DiagnosticsBundle | null }) {
  if (!data) return null;
  const { judge_bias, biased_judge_count, small_n_cells, essay_flags } = data;

  const total = biased_judge_count + small_n_cells.length + essay_flags.length;
  if (total === 0) return null;

  const banner = [
    `${biased_judge_count} judge${biased_judge_count === 1 ? "" : "s"} biased (|A%−50|>5pp)`,
    `${small_n_cells.length} small-n cell${small_n_cells.length === 1 ? "" : "s"}`,
    `${essay_flags.length} essay flag${essay_flags.length === 1 ? "" : "s"}`,
  ].join(" · ");

  return (
    <details className="collapsible diagnostics-pane">
      <summary>
        <span className="diagnostics-banner-label">Diagnostics</span>
        <span className="diagnostics-banner-body">{banner}</span>
      </summary>
      <div className="diagnostics-body">
        <DiagSectionA rows={judge_bias} />
        <DiagSectionB cells={small_n_cells} />
        {essay_flags.length > 0 && <DiagSectionC flags={essay_flags} />}
      </div>
    </details>
  );
}

function DiagSectionA({ rows }: { rows: DiagnosticsBundle["judge_bias"] }) {
  return (
    <section className="diag-section">
      <header className="diag-section-head">
        <h4>A · Judge bias breakdown</h4>
        <p className="versus-muted">
          All-rows A% vs completion-vs-completion A% (no human on either side). The gap
          estimates content-bias — how much having the human on one side pulls votes beyond
          pure position preference. Green 45–55%, amber 40–60%, red outside. Sorted by
          distance from 50%.
        </p>
      </header>
      <table className="log diag-bias-table">
        <thead>
          <tr>
            <th>judge</th>
            <th className="num">n</th>
            <th className="num">all A%</th>
            <th className="num ci">95% CI</th>
            <th className="num">cvc n</th>
            <th className="num">cvc A%</th>
            <th className="num ci">95% CI</th>
            <th className="num">content bias</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const allBand = pctBand(r.all_a_pct);
            const cvcBand = r.cvc_a_pct !== null ? pctBand(r.cvc_a_pct) : null;
            return (
              <tr key={r.judge_base}>
                <td className="versus-mono">{r.judge_base}</td>
                <td className="num">{r.n_total}</td>
                <td className={`num a-pct band-${allBand}`}>{fmtPct(r.all_a_pct)}</td>
                <td className="num ci versus-muted">
                  {fmtCI(r.all_ci_lo_pct, r.all_ci_hi_pct)}
                </td>
                <td className="num">{r.n_cvc || "—"}</td>
                <td
                  className={
                    cvcBand ? `num a-pct band-${cvcBand}` : "num versus-muted"
                  }
                >
                  {fmtPct(r.cvc_a_pct)}
                </td>
                <td className="num ci versus-muted">
                  {fmtCI(r.cvc_ci_lo_pct, r.cvc_ci_hi_pct)}
                </td>
                <td
                  className={
                    r.content_bias_pp === null
                      ? "num versus-muted"
                      : `num bias-${Math.abs(r.content_bias_pp) > 5 ? "high" : "low"}`
                  }
                >
                  {r.content_bias_pp === null
                    ? "—"
                    : `${r.content_bias_pp >= 0 ? "+" : ""}${r.content_bias_pp.toFixed(1)}pp`}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

function DiagSectionB({ cells }: { cells: DiagnosticsBundle["small_n_cells"] }) {
  if (cells.length === 0) {
    return (
      <section className="diag-section">
        <header className="diag-section-head">
          <h4>B · Small-n cells</h4>
        </header>
        <p className="versus-muted">No cells with n&lt;5. Every matrix cell has enough data.</p>
      </section>
    );
  }
  return (
    <section className="diag-section">
      <header className="diag-section-head">
        <h4>B · Small-n cells</h4>
        <p className="versus-muted">
          Cells with n&lt;5 — current conclusions for these tuples are not yet meaningful.
        </p>
      </header>
      <table className="log diag-small-table">
        <thead>
          <tr>
            <th className="num">n</th>
            <th>gen</th>
            <th>judge</th>
            <th>condition</th>
            <th>criterion</th>
          </tr>
        </thead>
        <tbody>
          {cells.map((c, i) => (
            <tr key={`${c.gen_model}|${c.judge_base}|${c.condition}|${c.criterion}|${i}`}>
              <td className="num small-n-cell">{c.n}</td>
              <td className="versus-mono">{c.gen_model}</td>
              <td className="versus-mono">{c.judge_base}</td>
              <td>
                <span className="versus-pill subtle">{c.condition}</span>
              </td>
              <td className="versus-mono">{c.criterion}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function DiagSectionC({ flags }: { flags: DiagnosticsBundle["essay_flags"] }) {
  return (
    <section className="diag-section">
      <header className="diag-section-head">
        <h4>C · Per-essay sanity</h4>
        <p className="versus-muted">
          Tie-rate &gt; 10%, or a single source winning every pair it appeared in. Sweeps can
          be legit dominance or a prefix/content leak — worth a look either way.
        </p>
      </header>
      <table className="log diag-essay-table">
        <thead>
          <tr>
            <th>essay</th>
            <th className="num">n</th>
            <th>tie rate</th>
            <th>sweep</th>
          </tr>
        </thead>
        <tbody>
          {flags.map((f) => (
            <tr key={f.essay_id}>
              <td className="versus-mono" title={f.title}>
                {f.essay_id}
              </td>
              <td className="num">{f.n_judgments}</td>
              <td className={f.tie_flag ? "tie-flag-hit" : "versus-muted"}>
                {fmtPct(f.tie_rate_pct)}
                {f.tie_flag && <span className="diag-flag-badge"> high</span>}
              </td>
              <td>
                {f.sweep_source ? (
                  <span className="sweep-hit">
                    <span className="versus-mono">{f.sweep_source}</span>{" "}
                    <span className="versus-muted">won {f.sweep_n}/{f.sweep_n}</span>
                  </span>
                ) : (
                  <span className="versus-muted">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
