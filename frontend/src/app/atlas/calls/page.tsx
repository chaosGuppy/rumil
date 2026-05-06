import Link from "next/link";
import type { CallTypeSummary } from "@/api";
import { atlasFetch } from "../_lib/fetch";
import { Crumbs } from "../_components/Crumbs";
import { Filter } from "../_components/Filter";

export const metadata = { title: "call types" };

export default async function CallsList() {
  const calls = await atlasFetch<CallTypeSummary[]>(
    "/api/atlas/registry/calls",
    [],
  );
  const sorted = calls.slice().sort((a, b) => a.call_type.localeCompare(b.call_type));

  const items = sorted.map((c) => ({
    searchKey: `${c.call_type} ${c.description ?? ""} ${c.runner_class ?? ""}`,
    node: <CallRow key={c.call_type} c={c} />,
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

function CallRow({ c }: { c: CallTypeSummary }) {
  return (
    <Link href={`/atlas/calls/${encodeURIComponent(c.call_type)}`} className="atlas-row">
      <div className="atlas-row-name">{c.call_type}</div>
      <div className="atlas-row-desc">{c.description}</div>
      <div className="atlas-row-meta">
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
