import Link from "next/link";
import { notFound } from "next/navigation";
import type {
  QuestionTrajectory,
  TrajectoryConsideration,
  TrajectoryJudgement,
  TrajectoryView,
} from "@/api";
import { atlasFetch } from "../../../../_lib/fetch";
import { Crumbs } from "../../../../_components/Crumbs";
import { CrossLink } from "../../../../_components/CrossLink";

export const metadata = { title: "trajectory" };

function fmtTs(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toISOString().slice(0, 16).replace("T", " ");
  } catch {
    return iso.slice(0, 16);
  }
}

export default async function TrajectoryPage({
  params,
}: {
  params: Promise<{ page_id: string }>;
}) {
  const { page_id } = await params;
  const traj = await atlasFetch<QuestionTrajectory | null>(
    `/api/atlas/pages/${encodeURIComponent(page_id)}/trajectory`,
    null,
  );
  if (!traj) notFound();

  const judgements = traj.judgements ?? [];
  const views = traj.views ?? [];
  const considerations = traj.considerations ?? [];
  const credences = traj.credences ?? [];

  // Group considerations by which judgement window they landed in
  const consByAfter: Record<string, TrajectoryConsideration[]> = {};
  const consUnanchored: TrajectoryConsideration[] = [];
  for (const c of considerations) {
    const key = c.landed_after_judgement_id;
    if (key) {
      if (!consByAfter[key]) consByAfter[key] = [];
      consByAfter[key].push(c);
    } else {
      consUnanchored.push(c);
    }
  }

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs
            items={[
              { label: "atlas", href: "/atlas" },
              { label: "pages" },
              { label: page_id.slice(0, 8) },
              { label: "trajectory" },
            ]}
          />
          <h1 className="is-sans">{traj.question_headline || "(no headline)"}</h1>
          {traj.question_abstract && (
            <p className="atlas-lede">{traj.question_abstract}</p>
          )}
          <div className="atlas-chip-row" style={{ marginTop: "0.5rem" }}>
            <span className="atlas-chip is-orchestrator">question</span>
            <CrossLink to={`/pages/${encodeURIComponent(page_id)}`}>
              page {page_id.slice(0, 8)}
            </CrossLink>
            <Link
              href={`/atlas/pages/${encodeURIComponent(page_id)}`}
              className="atlas-chip"
            >
              page provenance →
            </Link>
          </div>
        </div>
      </div>

      <div className="atlas-stat-grid" style={{ marginBottom: "1.5rem" }}>
        <div
          className="atlas-stat"
          title="distinct runs that produced a judgement, view, or consideration on this question"
        >
          <span className="atlas-stat-num">{traj.n_runs_touched}</span>
          <span className="atlas-stat-label">runs with output</span>
        </div>
        {(traj.n_runs_silent ?? 0) > 0 && (
          <div
            className="atlas-stat"
            title="runs that scoped at least one call to this question but produced no judgement/view/consideration"
            style={{
              background: "var(--a-warm-soft)",
              borderColor: "var(--a-warm)",
            }}
          >
            <span className="atlas-stat-num" style={{ color: "var(--a-warm)" }}>
              {traj.n_runs_silent}
            </span>
            <span className="atlas-stat-label">silent runs</span>
          </div>
        )}
        <div className="atlas-stat">
          <span className="atlas-stat-num">{traj.n_judgements}</span>
          <span className="atlas-stat-label">judgements</span>
        </div>
        <div className="atlas-stat">
          <span className="atlas-stat-num">{traj.n_views}</span>
          <span className="atlas-stat-label">views</span>
        </div>
        <div className="atlas-stat">
          <span className="atlas-stat-num">{traj.n_considerations}</span>
          <span className="atlas-stat-label">considerations</span>
        </div>
        {credences.length > 0 && (
          <div className="atlas-stat">
            <span className="atlas-stat-num">{traj.latest_credence ?? "—"}</span>
            <span className="atlas-stat-label">latest credence</span>
          </div>
        )}
        {credences.length > 0 && (
          <div className="atlas-stat">
            <span className="atlas-stat-num">
              {(traj.credence_volatility ?? 0).toFixed(2)}
            </span>
            <span className="atlas-stat-label">cred volatility</span>
          </div>
        )}
        {traj.converging != null && (
          <div className="atlas-stat">
            <span
              className="atlas-stat-num"
              style={{
                color: traj.converging ? "var(--a-success)" : "var(--a-warm)",
              }}
            >
              {traj.converging ? "converging" : "thrashing"}
            </span>
            <span className="atlas-stat-label">trajectory</span>
          </div>
        )}
      </div>

      {judgements.length === 0 && considerations.length === 0 && views.length === 0 && (
        <div className="atlas-empty">
          <strong>no trajectory yet</strong>
          this question has no judgements / views / considerations recorded.
        </div>
      )}

      {judgements.length > 0 && (
        <section className="atlas-section">
          <div className="atlas-section-head">
            <h2>judgement timeline</h2>
            <span className="atlas-section-meta">
              {judgements.length} judgements · considerations between each pair
              show what moved the answer
            </span>
          </div>
          <div className="atlas-trajectory">
            {judgements.map((j, i) => (
              <JudgementBlock
                key={j.page_id}
                j={j}
                idx={i}
                considerationsAfter={consByAfter[j.page_id] ?? []}
              />
            ))}
            {consUnanchored.length > 0 && (
              <div className="atlas-trajectory-cons-block">
                <div className="atlas-trajectory-cons-head">
                  before any judgement ({consUnanchored.length})
                </div>
                {consUnanchored.map((c) => (
                  <ConsiderationRow key={c.page_id} c={c} />
                ))}
              </div>
            )}
          </div>
        </section>
      )}

      {views.length > 0 && (
        <section className="atlas-section">
          <div className="atlas-section-head">
            <h2>views</h2>
            <span className="atlas-section-meta">
              {views.length} view {views.length === 1 ? "page" : "pages"}{" "}
              produced for this question
            </span>
          </div>
          <div className="atlas-rows">
            {views.map((v) => (
              <ViewRow key={v.page_id} v={v} />
            ))}
          </div>
        </section>
      )}

      {(traj.silent_run_ids ?? []).length > 0 && (
        <section className="atlas-section">
          <div className="atlas-section-head">
            <h2>silent runs</h2>
            <span className="atlas-section-meta">
              runs that scoped a call to this question but produced no
              judgement / view / consideration · {traj.silent_run_ids?.length ?? 0}
            </span>
          </div>
          <div className="atlas-chip-row">
            {(traj.silent_run_ids ?? []).map((rid) => (
              <Link
                key={rid}
                href={`/atlas/runs/${encodeURIComponent(rid)}/flow`}
                className="atlas-chip"
                title="run flow"
              >
                run {rid.slice(0, 8)} →
              </Link>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function JudgementBlock({
  j,
  idx,
  considerationsAfter,
}: {
  j: TrajectoryJudgement;
  idx: number;
  considerationsAfter: TrajectoryConsideration[];
}) {
  const dc = j.delta_credence;
  const dr = j.delta_robustness;
  return (
    <div className="atlas-trajectory-judgement">
      <div className="atlas-trajectory-jhead">
        <span className="atlas-trajectory-jindex">J{idx + 1}</span>
        <span className="atlas-trajectory-jts">{fmtTs(j.created_at)}</span>
        {j.credence != null && (
          <span className="atlas-chip is-muted">
            cred {j.credence}/9
            {dc != null && dc !== 0 && (
              <span
                style={{
                  marginLeft: 4,
                  color: dc > 0 ? "var(--a-success)" : "var(--a-warm)",
                }}
              >
                {dc > 0 ? `↑${dc}` : `↓${Math.abs(dc)}`}
              </span>
            )}
          </span>
        )}
        {j.robustness != null && (
          <span className="atlas-chip is-muted">
            rob {j.robustness}/5
            {dr != null && dr !== 0 && (
              <span
                style={{
                  marginLeft: 4,
                  color: dr > 0 ? "var(--a-success)" : "var(--a-warm)",
                }}
              >
                {dr > 0 ? `↑${dr}` : `↓${Math.abs(dr)}`}
              </span>
            )}
          </span>
        )}
        {j.call_type && (
          <Link
            href={`/atlas/calls/${encodeURIComponent(j.call_type)}`}
            className="atlas-chip"
          >
            {j.call_type}
          </Link>
        )}
        {j.run_id && (
          <Link
            href={`/atlas/runs/${encodeURIComponent(j.run_id)}/flow`}
            className="atlas-chip"
            title={j.run_name ?? ""}
          >
            run {j.run_id.slice(0, 8)} →
          </Link>
        )}
        <CrossLink to={`/pages/${encodeURIComponent(j.page_id)}`}>
          j {j.page_id.slice(0, 8)}
        </CrossLink>
      </div>
      <div className="atlas-trajectory-jheadline">{j.headline}</div>
      {j.abstract && (
        <div className="atlas-trajectory-jabstract">{j.abstract}</div>
      )}
      {j.credence_reasoning && (
        <div className="atlas-trajectory-jreasoning">
          <span style={{ color: "var(--a-muted)", marginRight: "0.4rem" }}>why:</span>
          {j.credence_reasoning}
        </div>
      )}

      {considerationsAfter.length > 0 && (
        <div className="atlas-trajectory-cons-block">
          <div className="atlas-trajectory-cons-head">
            after J{idx + 1}: {considerationsAfter.length} consideration
            {considerationsAfter.length === 1 ? "" : "s"} landed
          </div>
          {considerationsAfter.map((c) => (
            <ConsiderationRow key={c.page_id} c={c} />
          ))}
        </div>
      )}
    </div>
  );
}

function ConsiderationRow({ c }: { c: TrajectoryConsideration }) {
  const dir = c.direction;
  const dirGlyph =
    dir === "for" ? "✓" : dir === "against" ? "✗" : dir === "complicates" ? "≈" : "·";
  const dirColor =
    dir === "for"
      ? "var(--a-success)"
      : dir === "against"
        ? "var(--a-warm)"
        : "var(--a-muted)";
  return (
    <div className="atlas-trajectory-cons">
      <span className="atlas-trajectory-cons-glyph" style={{ color: dirColor }}>
        {dirGlyph}
      </span>
      <span className="atlas-trajectory-cons-meta">
        {c.strength != null && <span title="link strength">s={c.strength}</span>}
        {c.credence != null && <span title="claim credence">cred {c.credence}/9</span>}
        {c.robustness != null && <span title="claim robustness">rob {c.robustness}/5</span>}
        {c.call_type && (
          <Link href={`/atlas/calls/${encodeURIComponent(c.call_type)}`}>
            {c.call_type}
          </Link>
        )}
      </span>
      <span className="atlas-trajectory-cons-headline">
        <CrossLink to={`/pages/${encodeURIComponent(c.page_id)}`}>{c.headline}</CrossLink>
      </span>
    </div>
  );
}

function ViewRow({ v }: { v: TrajectoryView }) {
  return (
    <div className="atlas-trajectory-view">
      <span className="atlas-trajectory-jts">{fmtTs(v.created_at)}</span>
      <span className="atlas-chip is-versus">view</span>
      <span style={{ flex: 1 }}>{v.headline || "(no headline)"}</span>
      {v.run_id && (
        <Link
          href={`/atlas/runs/${encodeURIComponent(v.run_id)}/flow`}
          className="atlas-chip"
        >
          run {v.run_id.slice(0, 8)} →
        </Link>
      )}
      <CrossLink to={`/pages/${encodeURIComponent(v.page_id)}`}>
        v {v.page_id.slice(0, 8)}
      </CrossLink>
      {v.superseded_by && (
        <span className="atlas-chip is-warm" title={v.superseded_by}>
          superseded
        </span>
      )}
    </div>
  );
}
