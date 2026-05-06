import Link from "next/link";
import type { DispatchSummary } from "@/api";
import { atlasFetch } from "../_lib/fetch";
import { Crumbs } from "../_components/Crumbs";
import { Filter } from "../_components/Filter";

export const metadata = { title: "dispatches" };

export default async function DispatchesList() {
  const dispatches = await atlasFetch<DispatchSummary[]>(
    "/api/atlas/registry/dispatches",
    [],
  );

  const recurses = dispatches
    .filter((d) => d.is_recurse)
    .sort((a, b) => a.call_type.localeCompare(b.call_type));
  const regular = dispatches
    .filter((d) => !d.is_recurse)
    .sort((a, b) => a.call_type.localeCompare(b.call_type));

  const regularItems = regular.map((d) => ({
    searchKey: `${d.call_type} ${d.name} ${d.description ?? ""}`,
    node: <DispatchRow key={d.call_type} d={d} />,
  }));

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs items={[{ label: "atlas", href: "/atlas" }, { label: "dispatches" }]} />
          <h1 className="is-sans">dispatches</h1>
          <p className="atlas-lede">
            The tool schemas an orchestrator uses to plan the next call. Each
            dispatch&apos;s fields shape what context that call will receive.
          </p>
        </div>
      </div>

      <section className="atlas-section">
        <div className="atlas-section-head">
          <h2>regular dispatches</h2>
          <span className="atlas-section-meta">
            {regular.length} · plan the next call
          </span>
        </div>
        <Filter items={regularItems} placeholder="filter dispatches…" />
      </section>

      {recurses.length > 0 && (
        <section className="atlas-section">
          <div className="atlas-section-head">
            <h2>recurse dispatches</h2>
            <span className="atlas-section-meta">
              {recurses.length} · trigger sub-investigations
            </span>
          </div>
          <div className="atlas-rows">
            {recurses.map((d) => (
              <DispatchRow key={d.call_type} d={d} />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function DispatchRow({ d }: { d: DispatchSummary }) {
  return (
    <Link href={`/atlas/dispatches/${encodeURIComponent(d.call_type)}`} className="atlas-row">
      <div className="atlas-row-name">{d.call_type}</div>
      <div className="atlas-row-desc">{d.description}</div>
      <div className="atlas-row-meta">
        {d.is_recurse && <span className="atlas-chip is-flag">recurse</span>}
        <span className="atlas-chip is-muted">
          {(d.fields ?? []).length} fields
        </span>
      </div>
    </Link>
  );
}
