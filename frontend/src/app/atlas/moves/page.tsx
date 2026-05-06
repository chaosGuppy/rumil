import Link from "next/link";
import type { MoveSummary } from "@/api";
import { atlasFetch } from "../_lib/fetch";
import { Crumbs } from "../_components/Crumbs";
import { Filter } from "../_components/Filter";
import { IntensityBar } from "../_components/IntensityBar";

export const metadata = { title: "moves" };

export default async function MovesList() {
  const moves = await atlasFetch<MoveSummary[]>(
    "/api/atlas/registry/moves",
    [],
  );
  const sorted = moves.slice().sort(
    (a, b) =>
      (b.recent_invocations ?? 0) - (a.recent_invocations ?? 0) ||
      a.move_type.localeCompare(b.move_type),
  );
  const maxRecent = Math.max(1, ...sorted.map((m) => m.recent_invocations ?? 0));

  const items = sorted.map((m) => ({
    searchKey: `${m.name} ${m.move_type} ${m.description ?? ""}`,
    node: <MoveRow key={m.move_type} m={m} maxRecent={maxRecent} />,
  }));

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs items={[{ label: "atlas", href: "/atlas" }, { label: "moves" }]} />
          <h1 className="is-sans">moves</h1>
          <p className="atlas-lede">
            The tools made available to LLMs inside a call. Each carries a
            payload schema where every field has a description written for the
            model — those docstrings are this section&apos;s killer feature.
          </p>
        </div>
      </div>
      <Filter items={items} placeholder="filter moves…" />
    </div>
  );
}

function MoveRow({ m, maxRecent }: { m: MoveSummary; maxRecent: number }) {
  const usedCount = (m.used_in_call_types ?? []).length;
  const recent = m.recent_invocations ?? 0;
  // move_type is the unique identifier (e.g. CREATE_QUESTION vs
  // CREATE_SCOUT_QUESTION). Multiple move_types can share the same
  // ``name`` (the LLM-facing tool name) — surface move_type as the
  // primary label so the rows don't read as duplicates.
  const sameNameDifferentType = m.move_type.toLowerCase() !== m.name.toLowerCase();
  return (
    <Link href={`/atlas/moves/${encodeURIComponent(m.move_type)}`} className="atlas-row">
      <div className="atlas-row-name">
        {m.move_type}
        {sameNameDifferentType && (
          <span
            style={{
              color: "var(--a-muted)",
              marginLeft: "0.5rem",
              fontFamily: "var(--a-mono)",
              fontSize: "0.74rem",
              fontWeight: 400,
            }}
            title="LLM-facing tool name"
          >
            tool: {m.name}
          </span>
        )}
      </div>
      <div className="atlas-row-desc">
        <div style={{ marginBottom: "0.4rem" }}>{m.description}</div>
        <IntensityBar
          recent={recent}
          recentMax={maxRecent}
          recentLabel="executions in recent calls' trace_json"
        />
      </div>
      <div className="atlas-row-meta">
        <span
          className={`atlas-chip ${recent > 0 ? "is-accent" : "is-muted"}`}
          title="executions counted in recent calls' trace_json"
        >
          {recent}× recent
        </span>
        <span className="atlas-chip is-muted">
          {usedCount} call{usedCount === 1 ? "" : "s"}
        </span>
        <span className="atlas-chip is-muted">
          {(m.fields ?? []).length} fields
        </span>
      </div>
    </Link>
  );
}
