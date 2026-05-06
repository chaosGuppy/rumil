import Link from "next/link";
import type {
  OverlayCall,
  WorkflowOverlay,
  WorkflowOverlayStage,
  WorkflowStage,
} from "@/api";
import { fmtCost, fmtDuration } from "../_lib/format";

export function OverlayStages({
  overlay,
  currentStageId,
}: {
  overlay: WorkflowOverlay;
  currentStageId?: string | null;
}) {
  const profile = overlay.profile;
  const profileStages: WorkflowStage[] = profile?.stages ?? [];
  const overlayStages: WorkflowOverlayStage[] = overlay.stages ?? [];

  const profileById = new Map<string, WorkflowStage>();
  for (const s of profileStages) profileById.set(s.id, s);
  const overlayById = new Map<string, WorkflowOverlayStage>();
  for (const s of overlayStages) overlayById.set(s.stage_id, s);

  const orderedIds = [
    ...profileStages.map((s) => s.id),
    ...overlayStages
      .filter((s) => !profileById.has(s.stage_id))
      .map((s) => s.stage_id),
  ];

  return (
    <div>
      {orderedIds.map((sid) => (
        <OverlayStageBlock
          key={sid}
          stage_id={sid}
          overlay={overlayById.get(sid)}
          spec={profileById.get(sid)}
          isCurrent={sid === currentStageId}
        />
      ))}
    </div>
  );
}

function OverlayStageBlock({
  stage_id,
  overlay,
  spec,
  isCurrent,
}: {
  stage_id: string;
  overlay?: WorkflowOverlayStage;
  spec?: WorkflowStage;
  isCurrent?: boolean;
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
    isCurrent ? "is-current" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={cls}>
      <div className="atlas-overlay-stage-head">
        <span className="atlas-overlay-stage-id">{stage_id}</span>
        <span className="atlas-overlay-stage-label">{label}</span>
        {isCurrent && (
          <span className="atlas-chip is-accent" style={{ fontSize: "0.62rem" }}>
            ● current
          </span>
        )}
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
          {calls.length > 0 && (
            <span>
              {calls.length} call{calls.length === 1 ? "" : "s"}
            </span>
          )}
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
        <span>{fmtDuration(call.duration_seconds)}</span>
      </span>
    </Link>
  );
}
