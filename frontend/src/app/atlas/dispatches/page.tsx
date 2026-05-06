import Link from "next/link";
import type { DispatchSummary } from "@/api";
import { atlasFetch } from "../_lib/fetch";
import { Crumbs } from "../_components/Crumbs";
import { Filter } from "../_components/Filter";
import { IntensityBar } from "../_components/IntensityBar";

export const metadata = { title: "dispatches" };

function byIntensity(a: DispatchSummary, b: DispatchSummary): number {
  return (
    (b.recent_invocations ?? 0) - (a.recent_invocations ?? 0) ||
    (b.lifetime_invocations ?? 0) - (a.lifetime_invocations ?? 0) ||
    a.call_type.localeCompare(b.call_type)
  );
}

export default async function DispatchesList() {
  const dispatches = await atlasFetch<DispatchSummary[]>(
    "/api/atlas/registry/dispatches",
    [],
  );

  const recurses = dispatches.filter((d) => d.is_recurse).sort(byIntensity);
  const regular = dispatches.filter((d) => !d.is_recurse).sort(byIntensity);
  const all = [...regular, ...recurses];
  const maxRecent = Math.max(1, ...all.map((d) => d.recent_invocations ?? 0));
  const maxLifetime = Math.max(1, ...all.map((d) => d.lifetime_invocations ?? 0));

  const regularItems = regular.map((d) => ({
    searchKey: `${d.call_type} ${d.name} ${d.description ?? ""}`,
    node: (
      <DispatchRow
        key={d.call_type}
        d={d}
        maxRecent={maxRecent}
        maxLifetime={maxLifetime}
      />
    ),
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
            {regular.length} · plan the next call · sorted by recent intensity
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
              <DispatchRow
                key={d.call_type}
                d={d}
                maxRecent={maxRecent}
                maxLifetime={maxLifetime}
              />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function DispatchRow({
  d,
  maxRecent,
  maxLifetime,
}: {
  d: DispatchSummary;
  maxRecent: number;
  maxLifetime: number;
}) {
  const recent = d.recent_invocations ?? 0;
  const lifetime = d.lifetime_invocations ?? 0;
  return (
    <Link href={`/atlas/dispatches/${encodeURIComponent(d.call_type)}`} className="atlas-row">
      <div className="atlas-row-name">{d.call_type}</div>
      <div className="atlas-row-desc">
        <IntensityBar
          recent={recent}
          recentMax={maxRecent}
          recentLabel="recent exchanges with this call_type"
          lifetime={lifetime}
          lifetimeMax={maxLifetime}
          lifetimeLabel="lifetime calls with this call_type"
        />
      </div>
      <div className="atlas-row-meta">
        <span
          className={`atlas-chip ${recent > 0 ? "is-accent" : "is-muted"}`}
          title="recent dispatches of this call_type"
        >
          {recent}× recent
        </span>
        <span
          className="atlas-chip is-muted"
          title="all-time dispatches of this call_type"
        >
          {lifetime}× lifetime
        </span>
        {d.is_recurse && <span className="atlas-chip is-flag">recurse</span>}
        <span className="atlas-chip is-muted">
          {(d.fields ?? []).length} fields
        </span>
      </div>
    </Link>
  );
}
