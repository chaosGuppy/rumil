import Link from "next/link";
import { notFound } from "next/navigation";
import type {
  CallTypeStats,
  CallTypeSummary,
  DispatchSummary,
  WorkflowProfile,
  WorkflowSummary,
} from "@/api";
import { atlasFetch } from "../../_lib/fetch";
import { Crumbs } from "../../_components/Crumbs";
import { SchemaTable } from "../../_components/SchemaTable";
import { CompositionViewer } from "../../_components/CompositionViewer";
import { CallStatsPanel } from "../../_components/CallStatsPanel";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ call_type: string }>;
}) {
  const { call_type } = await params;
  return { title: call_type };
}

export default async function CallDetail({
  params,
  searchParams,
}: {
  params: Promise<{ call_type: string }>;
  searchParams: Promise<{ bucket?: string; since?: string }>;
}) {
  const { call_type } = await params;
  const sp = await searchParams;
  const bucket = sp.bucket;
  const since = sp.since;

  const ct = await atlasFetch<CallTypeSummary | null>(
    `/api/atlas/registry/calls/${encodeURIComponent(call_type)}`,
    null,
  );
  if (!ct) notFound();

  const statsQs = new URLSearchParams();
  if (bucket && bucket !== "off") statsQs.set("bucket", bucket);
  if (since) statsQs.set("since", since);
  const statsPath = `/api/atlas/calls/${encodeURIComponent(call_type)}/stats${statsQs.toString() ? `?${statsQs.toString()}` : ""}`;

  const [dispatch, stats, workflows] = await Promise.all([
    ct.has_dispatch
      ? atlasFetch<DispatchSummary | null>(
          `/api/atlas/registry/dispatches/${encodeURIComponent(call_type)}`,
          null,
        )
      : Promise.resolve(null),
    atlasFetch<CallTypeStats | null>(statsPath, null),
    atlasFetch<WorkflowSummary[]>("/api/atlas/workflows", []),
  ]);

  const workflowProfiles = await Promise.all(
    workflows.map((w) =>
      atlasFetch<WorkflowProfile | null>(
        `/api/atlas/workflows/${encodeURIComponent(w.name)}`,
        null,
      ),
    ),
  );
  const usedInWorkflows: Array<{ wf: WorkflowProfile; stageLabels: string[] }> = [];
  for (const wp of workflowProfiles) {
    if (!wp) continue;
    const stages = (wp.stages ?? []).filter((s) =>
      (s.available_dispatch_call_types ?? []).includes(call_type),
    );
    if (stages.length > 0) {
      usedInWorkflows.push({
        wf: wp,
        stageLabels: stages.map((s) => s.label),
      });
    }
  }

  const movesByPreset = ct.moves_by_preset ?? {};
  const presetNames = Object.keys(movesByPreset).sort();
  const composition = ct.composition;

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs
            items={[
              { label: "atlas", href: "/atlas" },
              { label: "calls", href: "/atlas/calls" },
              { label: ct.call_type },
            ]}
          />
          <h1>{ct.call_type}</h1>
          <div className="atlas-chip-row" style={{ marginBottom: "0.85rem" }}>
            {ct.has_dispatch && (
              <Link
                href={`/atlas/dispatches/${encodeURIComponent(ct.call_type)}`}
                className="atlas-chip is-accent"
              >
                has dispatch · {ct.dispatch_name ?? "—"} →
              </Link>
            )}
            {ct.runner_class && (
              <span className="atlas-chip">{ct.runner_class}</span>
            )}
          </div>
          <p className="atlas-lede">{ct.description}</p>
        </div>
      </div>

      <div className="atlas-split">
        <div>
          {dispatch && (
            <section className="atlas-section">
              <div className="atlas-section-head">
                <h2>dispatch payload</h2>
                <span className="atlas-section-meta">
                  {dispatch.fields?.length ?? 0} fields ·{" "}
                  <Link href={`/atlas/dispatches/${ct.call_type}`}>
                    full dispatch profile →
                  </Link>
                </span>
              </div>
              <SchemaTable fields={dispatch.fields ?? []} />
            </section>
          )}

          {composition && (composition.parts?.length ?? 0) > 0 && (
            <section className="atlas-section">
              <div className="atlas-section-head">
                <h2>prompt</h2>
                <span className="atlas-section-meta">
                  composed from {composition.parts.length} part
                  {composition.parts.length === 1 ? "" : "s"} · concise by default
                </span>
              </div>
              <CompositionViewer composition={composition} />
            </section>
          )}

          {stats && (
            <CallStatsPanel
              stats={stats}
              callType={ct.call_type}
              bucket={bucket}
            />
          )}

          {presetNames.length > 0 && (
            <section className="atlas-section">
              <div className="atlas-section-head">
                <h2>available moves</h2>
                <span className="atlas-section-meta">
                  by preset · {presetNames.length} preset
                  {presetNames.length === 1 ? "" : "s"}
                </span>
              </div>
              {presetNames.map((preset) => (
                <div key={preset} style={{ marginBottom: "0.9rem" }}>
                  <div
                    style={{
                      fontFamily: "var(--a-mono)",
                      fontSize: "0.7rem",
                      textTransform: "uppercase",
                      letterSpacing: "0.12em",
                      color: "var(--a-muted)",
                      marginBottom: "0.4rem",
                    }}
                  >
                    preset · {preset}
                  </div>
                  <div className="atlas-chip-row">
                    {(movesByPreset[preset] ?? []).map((m) => (
                      <Link
                        key={m}
                        href={`/atlas/moves/${encodeURIComponent(m)}`}
                        className="atlas-chip is-accent"
                      >
                        {m}
                      </Link>
                    ))}
                    {(movesByPreset[preset] ?? []).length === 0 && (
                      <span style={{ color: "var(--a-muted)", fontSize: "0.78rem" }}>
                        no moves
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </section>
          )}
        </div>

        <aside className="atlas-aside">
          <div className="atlas-aside-block">
            <h3>architecture</h3>
            <ul className="atlas-aside-list">
              {ct.runner_class && (
                <li><strong style={{ color: "var(--a-muted)" }}>runner:</strong> {ct.runner_class}</li>
              )}
              {ct.context_builder && (
                <li><strong style={{ color: "var(--a-muted)" }}>context:</strong> {ct.context_builder}</li>
              )}
              {ct.workspace_updater && (
                <li><strong style={{ color: "var(--a-muted)" }}>updater:</strong> {ct.workspace_updater}</li>
              )}
              {ct.closing_reviewer && (
                <li><strong style={{ color: "var(--a-muted)" }}>reviewer:</strong> {ct.closing_reviewer}</li>
              )}
            </ul>
          </div>

          <div className="atlas-aside-block">
            <h3>used in workflows</h3>
            <ul className="atlas-aside-list">
              {usedInWorkflows.length === 0 ? (
                <li style={{ color: "var(--a-muted)" }}>
                  not declared in any workflow stage
                </li>
              ) : (
                usedInWorkflows.map(({ wf, stageLabels }) => (
                  <li key={wf.name} style={{ fontFamily: "var(--a-sans)", fontSize: "0.83rem", lineHeight: 1.45 }}>
                    <Link href={`/atlas/workflows/${wf.name}`}>{wf.name}</Link>
                    <span style={{ color: "var(--a-muted)" }}>
                      {" "}· {stageLabels.join(", ")}
                    </span>
                  </li>
                ))
              )}
            </ul>
          </div>
        </aside>
      </div>
    </div>
  );
}
