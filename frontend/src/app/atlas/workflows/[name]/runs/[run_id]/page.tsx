import Link from "next/link";
import { notFound } from "next/navigation";
import type {
  OverlayCall,
  WorkflowOverlay,
  WorkflowOverlayStage,
  WorkflowStage,
} from "@/api";
import { atlasFetch } from "../../../../_lib/fetch";
import { Crumbs } from "../../../../_components/Crumbs";
import { CrossLink } from "../../../../_components/CrossLink";

export const metadata = { title: "workflow run overlay" };

function fmtCost(v: number | undefined | null): string {
  if (v == null) return "—";
  if (v >= 1) return `$${v.toFixed(2)}`;
  if (v >= 0.01) return `$${v.toFixed(3)}`;
  return `$${v.toFixed(4)}`;
}

function fmtDur(s: number | undefined | null): string {
  if (s == null) return "—";
  if (s < 60) return `${s.toFixed(1)}s`;
  if (s < 3600) return `${(s / 60).toFixed(1)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

function fmtTime(s: string | undefined | null): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleString();
  } catch {
    return s;
  }
}

export default async function WorkflowRunOverlay({
  params,
}: {
  params: Promise<{ name: string; run_id: string }>;
}) {
  const { name, run_id } = await params;
  const overlay = await atlasFetch<WorkflowOverlay | null>(
    `/api/atlas/workflows/${encodeURIComponent(name)}/runs/${encodeURIComponent(run_id)}/overlay`,
    null,
  );
  if (!overlay) notFound();

  const profile = overlay.profile;
  const profileStages: WorkflowStage[] = profile?.stages ?? [];
  const overlayStages: WorkflowOverlayStage[] = overlay.stages ?? [];

  // index profile stages by id, fall back to overlay-only stages (e.g. unmapped calls)
  const profileById = new Map<string, WorkflowStage>();
  for (const s of profileStages) profileById.set(s.id, s);
  const overlayById = new Map<string, WorkflowOverlayStage>();
  for (const s of overlayStages) overlayById.set(s.stage_id, s);

  // render in the profile's order, then any overlay stages with no matching profile entry
  const orderedIds = [
    ...profileStages.map((s) => s.id),
    ...overlayStages.filter((s) => !profileById.has(s.stage_id)).map((s) => s.stage_id),
  ];

  const totalCost = overlay.cost_usd ?? 0;
  const nCalls = overlay.n_calls ?? 0;
  const dur = overlay.duration_seconds;
  const isVersus = profile?.kind === "versus_workflow";

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs
            items={[
              { label: "atlas", href: "/atlas" },
              { label: "workflows", href: "/atlas/workflows" },
              { label: name, href: `/atlas/workflows/${name}` },
              { label: "runs" },
              { label: run_id.slice(0, 8) },
            ]}
          />
          <h1>{name} <span style={{ color: "var(--a-muted)", fontWeight: 400 }}>· {run_id.slice(0, 8)}</span></h1>
          <div className="atlas-chip-row" style={{ marginBottom: "0.85rem" }}>
            <span className={`atlas-chip ${isVersus ? "is-versus" : "is-orchestrator"}`}>
              {(profile?.kind ?? "workflow").replace("_", " ")}
            </span>
            <Link
              href={`/atlas/runs/${encodeURIComponent(run_id)}/flow`}
              className="atlas-chip is-accent"
            >
              run flow →
            </Link>
            <Link
              href={`/atlas/workflows/${encodeURIComponent(name)}`}
              className="atlas-chip"
            >
              workflow spec →
            </Link>
            <CrossLink to={`/traces/${run_id}`} chip>
              full trace
            </CrossLink>
          </div>
          <p className="atlas-lede">
            What actually happened on this run, painted onto the workflow&apos;s stage
            diagram. Stages with a teal stripe fired; dashed/muted stages were
            skipped or never reached.
          </p>
        </div>
      </div>

      <div className="atlas-stat-grid">
        <div className="atlas-stat">
          <span className="atlas-stat-num">{nCalls}</span>
          <span className="atlas-stat-label">calls</span>
        </div>
        <div className="atlas-stat">
          <span className="atlas-stat-num">{fmtCost(totalCost)}</span>
          <span className="atlas-stat-label">cost</span>
        </div>
        <div className="atlas-stat">
          <span className="atlas-stat-num">{fmtDur(dur)}</span>
          <span className="atlas-stat-label">duration</span>
        </div>
        <div className="atlas-stat">
          <span className="atlas-stat-num" style={{ fontSize: "0.95rem" }}>
            {fmtTime(overlay.started_at).split(",")[0]}
          </span>
          <span className="atlas-stat-label">started</span>
        </div>
      </div>

      <section className="atlas-section">
        <div className="atlas-section-head">
          <h2>stage timeline</h2>
          <span className="atlas-section-meta">
            {overlayStages.filter((s) => s.fired).length} fired ·{" "}
            {overlayStages.filter((s) => s.skipped).length} skipped ·{" "}
            {profileStages.length} declared
          </span>
        </div>

        <div>
          {orderedIds.map((sid) => {
            const ov = overlayById.get(sid);
            const spec = profileById.get(sid);
            return <OverlayStageBlock key={sid} stage_id={sid} overlay={ov} spec={spec} />;
          })}
        </div>
      </section>
    </div>
  );
}

function OverlayStageBlock({
  stage_id,
  overlay,
  spec,
}: {
  stage_id: string;
  overlay?: WorkflowOverlayStage;
  spec?: WorkflowStage;
}) {
  const fired = !!overlay?.fired;
  const skipped = !!overlay?.skipped;
  const isLoop = !!spec?.loop;
  const optional = !!spec?.optional;
  const iterations = overlay?.iterations ?? 0;
  const calls = overlay?.calls ?? [];
  const cost = overlay?.cost_usd ?? 0;
  const pages = overlay?.pages_loaded ?? 0;
  const label = overlay?.label || spec?.label || stage_id;

  const cls = [
    "atlas-overlay-stage",
    fired ? "is-fired" : "",
    skipped ? "is-skipped" : "",
    isLoop ? "is-loop" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={cls}>
      <div className="atlas-overlay-stage-head">
        <span className="atlas-overlay-stage-id">{stage_id}</span>
        <span className="atlas-overlay-stage-label">{label}</span>
        {fired && (
          <span className="atlas-chip is-success" style={{ fontSize: "0.62rem" }}>
            fired
          </span>
        )}
        {skipped && (
          <span className="atlas-chip is-muted" style={{ fontSize: "0.62rem" }}>
            skipped
          </span>
        )}
        {!fired && !skipped && !overlay && (
          <span className="atlas-chip is-muted" style={{ fontSize: "0.62rem" }}>
            no record
          </span>
        )}
        {optional && (
          <span className="atlas-chip is-flag" style={{ fontSize: "0.62rem" }}>
            optional
          </span>
        )}
        {isLoop && (
          <span className="atlas-chip is-flag" style={{ fontSize: "0.62rem" }}>
            ↻ loop
            {iterations > 0 && (
              <span style={{ color: "var(--a-muted)", marginLeft: "0.3rem" }}>
                × {iterations}
              </span>
            )}
          </span>
        )}
        <span className="atlas-overlay-stage-meta">
          {calls.length > 0 && <span>{calls.length} call{calls.length === 1 ? "" : "s"}</span>}
          {cost > 0 && <span>{fmtCost(cost)}</span>}
          {pages > 0 && <span>{pages}p</span>}
        </span>
      </div>

      {skipped && overlay?.skipped_reason && (
        <div className="atlas-overlay-skip-reason">
          skipped · {overlay.skipped_reason}
        </div>
      )}

      {spec?.description && !calls.length && (
        <div style={{ fontSize: "0.83rem", color: "var(--a-fg-soft)", lineHeight: 1.5 }}>
          {spec.description}
        </div>
      )}

      {calls.length > 0 && (
        <div className="atlas-overlay-calls-wrap">
          {calls.map((c) => (
            <CallRow key={c.call_id} call={c} />
          ))}
        </div>
      )}
    </div>
  );
}

function CallRow({ call }: { call: OverlayCall }) {
  return (
    <Link
      href={`/atlas/calls/${encodeURIComponent(call.call_type)}`}
      className="atlas-overlay-call"
    >
      <span className="atlas-overlay-call-id">{call.call_id.slice(0, 8)}</span>
      <span className="atlas-overlay-call-type">
        {call.call_type}
        <span
          className={`atlas-chip ${
            call.status === "complete"
              ? "is-success"
              : call.status === "error"
                ? "is-warm"
                : "is-muted"
          }`}
          style={{ marginLeft: "0.5rem", fontSize: "0.6rem" }}
        >
          {call.status}
        </span>
      </span>
      <span className="atlas-overlay-call-stats">
        {(call.pages_loaded ?? 0) > 0 && <span>{call.pages_loaded}p</span>}
        {(call.n_dispatches ?? 0) > 0 && <span>{call.n_dispatches}d</span>}
        <span>{fmtCost(call.cost_usd)}</span>
        <span>{fmtDur(call.duration_seconds)}</span>
      </span>
    </Link>
  );
}
