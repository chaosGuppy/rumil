import Link from "next/link";
import type { CallTypeSummary } from "@/api";
import { atlasFetch } from "../_lib/fetch";
import { Crumbs } from "../_components/Crumbs";
import { Filter } from "../_components/Filter";
import { IntensityBar } from "../_components/IntensityBar";

export const metadata = { title: "call types" };

export default async function CallsList() {
  const calls = await atlasFetch<CallTypeSummary[]>(
    "/api/atlas/registry/calls",
    [],
  );
  const sorted = calls.slice().sort(
    (a, b) =>
      (b.recent_invocations ?? 0) - (a.recent_invocations ?? 0) ||
      (b.lifetime_invocations ?? 0) - (a.lifetime_invocations ?? 0) ||
      a.call_type.localeCompare(b.call_type),
  );
  const maxRecent = Math.max(1, ...sorted.map((c) => c.recent_invocations ?? 0));
  const maxLifetime = Math.max(1, ...sorted.map((c) => c.lifetime_invocations ?? 0));

  const items = sorted.map((c) => ({
    searchKey: `${c.call_type} ${c.description ?? ""} ${c.runner_class ?? ""}`,
    node: (
      <CallRow
        key={c.call_type}
        c={c}
        maxRecent={maxRecent}
        maxLifetime={maxLifetime}
      />
    ),
  }));

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs items={[{ label: "atlas", href: "/atlas" }, { label: "calls" }]} />
          <h1 className="is-sans">call types</h1>
          <p className="atlas-lede">
            The unit of dispatch. Each pairs a context-builder with a
            workspace-updater and a closing-reviewer; some carry a dispatch
            tool that orchestrators use to plan them.
          </p>
        </div>
      </div>

      <Filter items={items} placeholder="filter call types…" />
    </div>
  );
}

function CallRow({
  c,
  maxRecent,
  maxLifetime,
}: {
  c: CallTypeSummary;
  maxRecent: number;
  maxLifetime: number;
}) {
  const recent = c.recent_invocations ?? 0;
  const lifetime = c.lifetime_invocations ?? 0;
  return (
    <Link href={`/atlas/calls/${encodeURIComponent(c.call_type)}`} className="atlas-row">
      <div className="atlas-row-name">{c.call_type}</div>
      <div className="atlas-row-desc">
        <IntensityBar
          recent={recent}
          recentMax={maxRecent}
          recentLabel="recent exchanges"
          lifetime={lifetime}
          lifetimeMax={maxLifetime}
          lifetimeLabel="lifetime calls"
        />
      </div>
      <div className="atlas-row-meta">
        <span
          className={`atlas-chip ${recent > 0 ? "is-accent" : "is-muted"}`}
          title="recent_invocations: count of recent exchanges with this call_type"
        >
          {recent}× recent
        </span>
        <span
          className="atlas-chip is-muted"
          title="lifetime_invocations: all-time call count for this call_type"
        >
          {lifetime}× lifetime
        </span>
        {c.has_dispatch && (
          <span className="atlas-chip is-accent">dispatch</span>
        )}
        {c.runner_class && (
          <span className="atlas-chip is-muted">{c.runner_class}</span>
        )}
      </div>
    </Link>
  );
}
