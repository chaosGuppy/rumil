import Link from "next/link";
import { notFound } from "next/navigation";
import type { InvocationIndex, MoveStats, MoveSummary } from "@/api";
import { atlasFetch } from "../../_lib/fetch";
import { Crumbs } from "../../_components/Crumbs";
import { SchemaTable } from "../../_components/SchemaTable";
import { MoveStatsPanel } from "../../_components/MoveStatsPanel";
import { InvocationsList } from "../../_components/InvocationsList";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ move_type: string }>;
}) {
  const { move_type } = await params;
  return { title: move_type };
}

export default async function MoveDetail({
  params,
  searchParams,
}: {
  params: Promise<{ move_type: string }>;
  searchParams: Promise<{ project_id?: string }>;
}) {
  const { move_type } = await params;
  const sp = await searchParams;
  const projectId = sp.project_id;

  const invQs = new URLSearchParams({ limit: "10" });
  if (projectId) invQs.set("project_id", projectId);
  const invocationsPath = `/api/atlas/moves/${encodeURIComponent(move_type)}/invocations?${invQs.toString()}`;

  const [m, stats, invocations] = await Promise.all([
    atlasFetch<MoveSummary | null>(
      `/api/atlas/registry/moves/${encodeURIComponent(move_type)}`,
      null,
    ),
    atlasFetch<MoveStats | null>(
      `/api/atlas/moves/${encodeURIComponent(move_type)}/stats`,
      null,
    ),
    atlasFetch<InvocationIndex | null>(invocationsPath, null),
  ]);
  if (!m) notFound();

  const usedIn = m.used_in_call_types ?? [];
  const presets = m.used_in_presets ?? [];

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs
            items={[
              { label: "atlas", href: "/atlas" },
              { label: "moves", href: "/atlas/moves" },
              { label: m.name },
            ]}
          />
          <h1>{m.name}</h1>
          <div className="atlas-chip-row" style={{ marginBottom: "0.85rem" }}>
            <span className="atlas-chip is-muted">{m.move_type}</span>
            <span className="atlas-chip is-muted">{(m.fields ?? []).length} fields</span>
            <span className="atlas-chip is-muted">
              {usedIn.length} call{usedIn.length === 1 ? "" : "s"}
            </span>
          </div>
          <p className="atlas-lede">{m.description}</p>
        </div>
      </div>

      <div className="atlas-split">
        <div>
          <section className="atlas-section">
            <div className="atlas-section-head">
              <h2>payload schema</h2>
              <span className="atlas-section-meta">
                each field&apos;s description is what the model sees
              </span>
            </div>
            <SchemaTable fields={m.fields ?? []} />
          </section>

          {stats && <MoveStatsPanel stats={stats} />}

          <InvocationsList
            index={invocations}
            kind="move"
            showCallTypeChip
          />
        </div>

        <aside className="atlas-aside">
          {m.code_path && (
            <div className="atlas-aside-block">
              <h3>code path</h3>
              <span className="atlas-codepath">{m.code_path}</span>
            </div>
          )}
          <div className="atlas-aside-block">
            <h3>used in call types</h3>
            <div className="atlas-chip-row">
              {usedIn.length === 0 && (
                <span style={{ color: "var(--a-muted)", fontSize: "0.78rem" }}>
                  not currently used
                </span>
              )}
              {usedIn.map((c) => (
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
          <div className="atlas-aside-block">
            <h3>presets</h3>
            <div className="atlas-chip-row">
              {presets.length === 0 && (
                <span style={{ color: "var(--a-muted)", fontSize: "0.78rem" }}>
                  no presets include this move
                </span>
              )}
              {presets.map((p) => (
                <span key={p} className="atlas-chip is-muted">{p}</span>
              ))}
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
