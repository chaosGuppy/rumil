import Link from "next/link";
import { notFound } from "next/navigation";
import type { DispatchSummary } from "@/api";
import { atlasFetch } from "../../_lib/fetch";
import { Crumbs } from "../../_components/Crumbs";
import { SchemaTable } from "../../_components/SchemaTable";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ call_type: string }>;
}) {
  const { call_type } = await params;
  return { title: `dispatch · ${call_type}` };
}

export default async function DispatchDetail({
  params,
}: {
  params: Promise<{ call_type: string }>;
}) {
  const { call_type } = await params;
  const d = await atlasFetch<DispatchSummary | null>(
    `/api/atlas/registry/dispatches/${encodeURIComponent(call_type)}`,
    null,
  );
  if (!d) notFound();

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs
            items={[
              { label: "atlas", href: "/atlas" },
              { label: "dispatches", href: "/atlas/dispatches" },
              { label: d.call_type },
            ]}
          />
          <h1>{d.name}</h1>
          <div className="atlas-chip-row" style={{ marginBottom: "0.85rem" }}>
            <span className="atlas-chip is-muted">dispatches → {d.call_type}</span>
            {d.is_recurse && <span className="atlas-chip is-flag">recurse</span>}
            {!d.is_recurse && (
              <Link
                href={`/atlas/calls/${encodeURIComponent(d.call_type)}`}
                className="atlas-chip is-accent"
              >
                call type profile →
              </Link>
            )}
          </div>
          <p className="atlas-lede">{d.description}</p>
        </div>
      </div>

      <section className="atlas-section">
        <div className="atlas-section-head">
          <h2>payload schema</h2>
          <span className="atlas-section-meta">
            {(d.fields ?? []).length} fields · what the orchestrator must
            specify when dispatching
          </span>
        </div>
        <SchemaTable fields={d.fields ?? []} />
      </section>
    </div>
  );
}
