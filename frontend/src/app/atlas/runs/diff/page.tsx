import Link from "next/link";
import type { RunDiff, RunDiffSide, StageDiffRow, DispatchCountDiff } from "@/api";
import { atlasFetch } from "../../_lib/fetch";
import { Crumbs } from "../../_components/Crumbs";
import { fmtCost, fmtDuration, fmtWhen, fmtDelta } from "../../_lib/format";
import { DiffPicker } from "./Picker";

export const metadata = { title: "run diff" };

function StripeFor({
  fired,
  skipped,
}: {
  fired?: boolean;
  skipped?: boolean;
}) {
  if (fired) return <span className="atlas-diff-stripe is-fired" title="fired" />;
  if (skipped) return <span className="atlas-diff-stripe is-skipped" title="skipped" />;
  return <span className="atlas-diff-stripe is-none" title="no record" />;
}

function NumPair({
  a,
  b,
  fmt = (n: number) => `${n}`,
}: {
  a: number | undefined;
  b: number | undefined;
  fmt?: (n: number) => string;
}) {
  const av = a ?? 0;
  const bv = b ?? 0;
  const delta = fmtDelta(av, bv, fmt);
  return (
    <div className="atlas-diff-cell">
      <div className="atlas-diff-cell-pair">
        <span className="atlas-diff-cell-a">{fmt(av)}</span>
        <span className="atlas-diff-cell-sep">→</span>
        <span className="atlas-diff-cell-b">{fmt(bv)}</span>
      </div>
      <span className={`atlas-diff-cell-delta ${delta.cls}`}>{delta.label}</span>
    </div>
  );
}

function SideHeader({ side, kind }: { side: RunDiffSide; kind: "a" | "b" }) {
  return (
    <div className={`atlas-diff-side is-${kind}`}>
      <div className="atlas-diff-side-label">side {kind.toUpperCase()}</div>
      <div className="atlas-diff-side-name">
        <Link href={`/atlas/runs/${side.run_id}/flow`}>
          {side.name || side.run_id.slice(0, 8)}
        </Link>
      </div>
      <div style={{ fontFamily: "var(--a-mono)", fontSize: "0.7rem", color: "var(--a-muted)", wordBreak: "break-all" }}>
        {side.run_id}
      </div>
      <div className="atlas-diff-side-stats">
        <span className="lbl">workflow</span>
        <span>{side.workflow_name ?? "—"}</span>
        <span className="lbl">cost</span>
        <span>{fmtCost(side.cost_usd ?? 0)}</span>
        <span className="lbl">calls</span>
        <span>{side.n_calls ?? 0}</span>
        <span className="lbl">disp</span>
        <span>{side.n_dispatches ?? 0}</span>
        <span className="lbl">pages</span>
        <span>{side.pages_loaded ?? 0}</span>
        <span className="lbl">dur</span>
        <span>{fmtDuration(side.duration_seconds)}</span>
        <span className="lbl">started</span>
        <span>{fmtWhen(side.started_at)}</span>
      </div>
    </div>
  );
}

export default async function RunDiffPage({
  searchParams,
}: {
  searchParams: Promise<{ a?: string; b?: string }>;
}) {
  const sp = await searchParams;
  const a = sp.a;
  const b = sp.b;

  let diff: RunDiff | null = null;
  if (a && b) {
    const qs = new URLSearchParams({ a, b });
    diff = await atlasFetch<RunDiff | null>(
      `/api/atlas/runs/diff?${qs.toString()}`,
      null,
    );
  }

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs
            items={[
              { label: "atlas", href: "/atlas" },
              { label: "runs" },
              { label: "diff" },
            ]}
          />
          <h1>run diff</h1>
          <p className="atlas-lede">
            Two runs side-by-side. Aligned on workflow stages where possible;
            divergent rows are highlighted, dispatch deltas at the bottom.
          </p>
        </div>
      </div>

      <DiffPicker a={a} b={b} />

      {!a || !b ? (
        <div className="atlas-empty">
          <strong>pick two runs</strong>
          paste a pair of run_ids above, or follow{" "}
          <span className="atlas-mono">compare with…</span> from any run row.
        </div>
      ) : !diff ? (
        <div className="atlas-empty">
          <strong>could not load diff</strong>
          one or both runs may not exist, or the API is unreachable.
        </div>
      ) : (
        <DiffBody diff={diff} />
      )}
    </div>
  );
}

function DiffBody({ diff }: { diff: RunDiff }) {
  const stages: StageDiffRow[] = diff.stages ?? [];
  const dispatches: DispatchCountDiff[] = diff.dispatch_diffs ?? [];
  const notes = diff.notes ?? [];
  const sameWorkflow = diff.same_workflow;

  const maxDispatch = Math.max(
    1,
    ...dispatches.map((d) => Math.max(d.a_count ?? 0, d.b_count ?? 0)),
  );

  return (
    <>
      <div className="atlas-diff-headers">
        <SideHeader side={diff.a} kind="a" />
        <div className="atlas-diff-vs">vs</div>
        <SideHeader side={diff.b} kind="b" />
      </div>

      {!sameWorkflow ? (
        <div className="atlas-callout">
          <strong style={{ display: "block", marginBottom: "0.3rem", fontFamily: "var(--a-mono)", fontSize: "0.7rem", textTransform: "uppercase", letterSpacing: "0.12em" }}>
            different workflows
          </strong>
          a is{" "}
          <span className="atlas-mono">{diff.a.workflow_name ?? "—"}</span>, b is{" "}
          <span className="atlas-mono">{diff.b.workflow_name ?? "—"}</span> — stage
          alignment skipped. The header cards above and the notes below are
          all you get.
        </div>
      ) : (
        <section className="atlas-section">
          <div className="atlas-section-head">
            <h2>aligned stages</h2>
            <span className="atlas-section-meta">
              {stages.length} stage{stages.length === 1 ? "" : "s"} ·{" "}
              {stages.filter((s) => !!s.a_fired !== !!s.b_fired).length} divergent ·
              workflow{" "}
              <span className="atlas-mono">{diff.aligned_workflow ?? diff.a.workflow_name ?? ""}</span>
            </span>
          </div>
          <div className="atlas-diff-table">
            <div className="atlas-diff-row is-head">
              <span>stage</span>
              <span>a / b</span>
              <span style={{ textAlign: "right" }}>iter</span>
              <span style={{ textAlign: "right" }}>cost</span>
              <span style={{ textAlign: "right" }}>pages</span>
              <span style={{ textAlign: "right" }}>calls</span>
            </div>
            {stages.map((s) => {
              const divergent = !!s.a_fired !== !!s.b_fired;
              return (
                <div
                  key={s.stage_id}
                  className={`atlas-diff-row ${divergent ? "is-divergent" : ""}`}
                >
                  <div className="atlas-diff-stage-label">
                    <span className="atlas-diff-stage-name">{s.label}</span>
                    <span className="atlas-diff-stage-id">{s.stage_id}</span>
                  </div>
                  <div className="atlas-diff-stripes" title="a · b">
                    <StripeFor fired={s.a_fired} skipped={s.a_skipped} />
                    <StripeFor fired={s.b_fired} skipped={s.b_skipped} />
                  </div>
                  <NumPair a={s.a_iterations} b={s.b_iterations} />
                  <NumPair a={s.a_cost_usd} b={s.b_cost_usd} fmt={fmtCost} />
                  <NumPair a={s.a_pages_loaded} b={s.b_pages_loaded} />
                  <NumPair a={s.a_n_calls} b={s.b_n_calls} />
                </div>
              );
            })}
            {stages.length === 0 && (
              <div style={{ padding: "0.85rem 1rem", fontFamily: "var(--a-mono)", fontSize: "0.74rem", color: "var(--a-muted)" }}>
                no stages
              </div>
            )}
          </div>
        </section>
      )}

      {dispatches.length > 0 && (
        <section className="atlas-section">
          <div className="atlas-section-head">
            <h2>dispatch counts</h2>
            <span className="atlas-section-meta">
              by call type · sorted by max(a, b)
            </span>
          </div>
          <div style={{ border: "1px solid var(--a-line)", background: "var(--a-bg-paper)" }}>
            {dispatches
              .slice()
              .sort(
                (x, y) =>
                  Math.max(y.a_count ?? 0, y.b_count ?? 0) -
                  Math.max(x.a_count ?? 0, x.b_count ?? 0),
              )
              .map((d) => {
                const av = d.a_count ?? 0;
                const bv = d.b_count ?? 0;
                return (
                  <div key={d.call_type} className="atlas-diff-dispatch">
                    <Link
                      href={`/atlas/calls/${encodeURIComponent(d.call_type)}`}
                      className="atlas-diff-dispatch-name"
                    >
                      {d.call_type}
                    </Link>
                    <div className="atlas-diff-dispatch-bar is-a-bar">
                      <span style={{ width: `${(av / maxDispatch) * 100}%` }} />
                    </div>
                    <div className="atlas-diff-dispatch-bar is-b-bar">
                      <span style={{ width: `${(bv / maxDispatch) * 100}%` }} />
                    </div>
                    <div className="atlas-diff-dispatch-counts">
                      <span className="a">{av}</span>
                      <span className="atlas-diff-cell-sep">→</span>
                      <span className="b">{bv}</span>
                    </div>
                  </div>
                );
              })}
          </div>
        </section>
      )}

      {notes.length > 0 && (
        <ul className="atlas-diff-notes">
          {notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}
    </>
  );
}
