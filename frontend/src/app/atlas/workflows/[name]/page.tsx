import Link from "next/link";
import { notFound } from "next/navigation";
import type { WorkflowProfile, WorkflowStage } from "@/api";
import { atlasFetch } from "../../_lib/fetch";
import { Crumbs } from "../../_components/Crumbs";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ name: string }>;
}) {
  const { name } = await params;
  return { title: name };
}

export default async function WorkflowDetail({
  params,
}: {
  params: Promise<{ name: string }>;
}) {
  const { name } = await params;
  const wf = await atlasFetch<WorkflowProfile | null>(
    `/api/atlas/workflows/${encodeURIComponent(name)}`,
    null,
  );
  if (!wf) notFound();

  const stages = wf.stages ?? [];
  const isVersus = wf.kind === "versus_workflow";

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs
            items={[
              { label: "atlas", href: "/atlas" },
              { label: "workflows", href: "/atlas/workflows" },
              { label: wf.name },
            ]}
          />
          <h1>{wf.name}</h1>
          <div className="atlas-chip-row" style={{ marginBottom: "0.85rem" }}>
            <span className={`atlas-chip ${isVersus ? "is-versus" : "is-orchestrator"}`}>
              {wf.kind.replace("_", " ")}
            </span>
            <Link
              href={`/atlas/workflows/${wf.name}/aggregate`}
              className="atlas-chip is-accent"
            >
              aggregate behavior →
            </Link>
            {(wf.recurses_into ?? []).map((r) => (
              <Link
                key={r}
                href={`/atlas/workflows/${r}`}
                className="atlas-chip is-orchestrator"
              >
                ↻ recurses into {r}
              </Link>
            ))}
          </div>
          <p className="atlas-lede">{wf.summary}</p>
        </div>
      </div>

      <div className="atlas-split">
        <div>
          <section className="atlas-section">
            <div className="atlas-section-head">
              <h2>stage diagram</h2>
              <span className="atlas-section-meta">
                {stages.length} stages · vertical pipeline
              </span>
            </div>
            <div className="atlas-pipeline">
              {stages.length === 0 ? (
                <div className="atlas-empty">
                  <strong>no stages declared</strong>
                  this workflow profile has no stages.
                </div>
              ) : (
                stages.map((s, i) => (
                  <StageBlock key={s.id} stage={s} isLast={i === stages.length - 1} />
                ))
              )}
            </div>
          </section>
        </div>

        <aside className="atlas-aside">
          <div className="atlas-aside-block">
            <h3>code paths</h3>
            <ul className="atlas-aside-list">
              {(wf.code_paths ?? []).map((p) => (
                <li key={p}><span className="atlas-codepath">{p}</span></li>
              ))}
              {!(wf.code_paths ?? []).length && (
                <li style={{ color: "var(--a-muted)" }}>none recorded</li>
              )}
            </ul>
          </div>

          {wf.relevant_settings && wf.relevant_settings.length > 0 && (
            <div className="atlas-aside-block">
              <h3>relevant settings</h3>
              <ul className="atlas-aside-list">
                {wf.relevant_settings.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
            </div>
          )}

          {wf.fingerprint_keys && wf.fingerprint_keys.length > 0 && (
            <div className="atlas-aside-block">
              <h3>fingerprint keys</h3>
              <ul className="atlas-aside-list">
                {wf.fingerprint_keys.map((k) => (
                  <li key={k}>{k}</li>
                ))}
              </ul>
            </div>
          )}

          {wf.notes && wf.notes.length > 0 && (
            <div className="atlas-aside-block">
              <h3>notes</h3>
              <ul
                className="atlas-aside-list"
                style={{ fontFamily: "var(--a-sans)", fontSize: "0.83rem", lineHeight: 1.5 }}
              >
                {wf.notes.map((n, i) => (
                  <li key={i} style={{ wordBreak: "normal" }}>{n}</li>
                ))}
              </ul>
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}

function StageBlock({ stage, isLast }: { stage: WorkflowStage; isLast: boolean }) {
  const optional = !!stage.optional;
  const isLoop = !!stage.loop;
  const recurses = stage.recurses_into ?? [];
  const cls = [
    "atlas-pipeline-stage",
    optional ? "is-optional" : "",
    isLoop ? "is-loop" : "",
    recurses.length ? "is-recurses" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <>
      <div className={cls}>
        {isLoop && (
          <div className="atlas-pipeline-loop-banner">
            <span>↻ loop</span>
            <span style={{ color: "var(--a-muted)" }}>
              {stage.branch_condition
                ? `repeats while ${stage.branch_condition}`
                : "repeats until terminal condition"}
            </span>
          </div>
        )}
        <div className="atlas-stage-id">{stage.id}</div>
        <div className="atlas-stage-label">{stage.label}</div>
        <div className="atlas-chip-row" style={{ marginBottom: "0.6rem" }}>
          {optional && (
            <span className="atlas-chip is-flag">optional</span>
          )}
          {isLoop && <span className="atlas-chip is-flag">loop</span>}
          {stage.branch_condition && !isLoop && (
            <span className="atlas-chip is-flag">
              ⤳ {stage.branch_condition}
            </span>
          )}
          {recurses.map((r) => (
            <Link
              key={r}
              href={`/atlas/workflows/${r}`}
              className="atlas-chip is-orchestrator"
            >
              ↻ recurses into {r}
            </Link>
          ))}
        </div>
        {stage.description && (
          <div className="atlas-stage-desc">{stage.description}</div>
        )}
        {stage.prompt_files && stage.prompt_files.length > 0 && (
          <div className="atlas-stage-block">
            <div className="atlas-stage-block-label">prompt files</div>
            <div className="atlas-chip-row">
              {stage.prompt_files.map((p) => (
                <Link
                  key={p}
                  href={`/atlas/prompts/${encodeURIComponent(p)}`}
                  className="atlas-chip"
                >
                  {p}
                </Link>
              ))}
            </div>
          </div>
        )}
        {stage.available_dispatch_call_types &&
          stage.available_dispatch_call_types.length > 0 && (
            <div className="atlas-stage-block">
              <div className="atlas-stage-block-label">
                available dispatches ({stage.available_dispatch_call_types.length})
              </div>
              <div className="atlas-chip-row">
                {stage.available_dispatch_call_types.map((c) => (
                  <Link
                    key={c}
                    href={`/atlas/calls/${encodeURIComponent(c)}`}
                    className="atlas-chip is-accent"
                  >
                    {c}
                  </Link>
                ))}
              </div>
            </div>
          )}
        {stage.available_move_types && stage.available_move_types.length > 0 && (
          <div className="atlas-stage-block">
            <div className="atlas-stage-block-label">
              available moves ({stage.available_move_types.length})
            </div>
            <div className="atlas-chip-row">
              {stage.available_move_types.map((m) => (
                <Link
                  key={m}
                  href={`/atlas/moves/${encodeURIComponent(m)}`}
                  className="atlas-chip"
                >
                  {m}
                </Link>
              ))}
            </div>
          </div>
        )}
        {stage.note && (
          <div
            style={{
              marginTop: "0.7rem",
              fontSize: "0.78rem",
              color: "var(--a-muted)",
              fontStyle: "italic",
            }}
          >
            note · {stage.note}
          </div>
        )}
      </div>
      {!isLast && <div className="atlas-stage-arrow">↓</div>}
    </>
  );
}
