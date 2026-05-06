import Link from "next/link";
import type { PromptIndex, PromptListItem } from "@/api";
import { atlasFetch } from "../_lib/fetch";
import { Crumbs } from "../_components/Crumbs";
import { Filter } from "../_components/Filter";

export const metadata = { title: "prompts" };

type SortKey = "intensity" | "compositions" | "size" | "name";

const SORTS: { key: SortKey; label: string; title: string }[] = [
  {
    key: "intensity",
    label: "use intensity",
    title: "recent_invocations · n_compositions · name",
  },
  {
    key: "compositions",
    label: "compositions",
    title: "static reach: how many call-type compositions reference this file",
  },
  { key: "size", label: "size", title: "char_count" },
  { key: "name", label: "name", title: "alphabetical" },
];

export default async function PromptsList({
  searchParams,
}: {
  searchParams: Promise<{ sort?: string }>;
}) {
  const sp = await searchParams;
  const sort: SortKey =
    (SORTS.find((s) => s.key === sp.sort)?.key ?? "intensity") as SortKey;

  const idx = await atlasFetch<PromptIndex>(
    "/api/atlas/registry/prompts_index?scan=500",
    { items: [], n_scanned_exchanges: 0 },
  );

  const all = idx.items ?? [];
  const sorted = [...all];
  if (sort === "intensity") {
    sorted.sort(
      (a, b) =>
        (b.recent_invocations ?? 0) - (a.recent_invocations ?? 0) ||
        (b.n_compositions ?? 0) - (a.n_compositions ?? 0) ||
        a.name.localeCompare(b.name),
    );
  } else if (sort === "compositions") {
    sorted.sort(
      (a, b) =>
        (b.n_compositions ?? 0) - (a.n_compositions ?? 0) ||
        (b.recent_invocations ?? 0) - (a.recent_invocations ?? 0) ||
        a.name.localeCompare(b.name),
    );
  } else if (sort === "size") {
    sorted.sort(
      (a, b) => (b.char_count ?? 0) - (a.char_count ?? 0) || a.name.localeCompare(b.name),
    );
  } else {
    sorted.sort((a, b) => a.name.localeCompare(b.name));
  }

  const maxRecent = Math.max(1, ...sorted.map((p) => p.recent_invocations ?? 0));

  const items = sorted.map((p) => ({
    searchKey: p.name,
    node: <PromptRow key={p.name} p={p} maxRecent={maxRecent} />,
  }));

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs items={[{ label: "atlas", href: "/atlas" }, { label: "prompts" }]} />
          <h1 className="is-sans">prompt files</h1>
        </div>
      </div>

      <div className="atlas-chip-row" style={{ marginBottom: "0.85rem" }}>
        <span
          style={{
            color: "var(--a-muted)",
            fontSize: "0.72rem",
            marginRight: "0.4rem",
          }}
        >
          sort:
        </span>
        {SORTS.map((s) => (
          <Link
            key={s.key}
            href={s.key === "intensity" ? "/atlas/prompts" : `/atlas/prompts?sort=${s.key}`}
            className={`atlas-chip ${sort === s.key ? "is-accent" : ""}`}
            title={s.title}
          >
            {s.label}
          </Link>
        ))}
      </div>

      <Filter items={items} placeholder="filter prompt files…" />
    </div>
  );
}

function PromptRow({ p, maxRecent }: { p: PromptListItem; maxRecent: number }) {
  const recent = p.recent_invocations ?? 0;
  const comps = p.n_compositions ?? 0;
  const sections = p.n_sections ?? 0;
  const widthPct = recent > 0 ? Math.max(2, Math.round((100 * recent) / maxRecent)) : 0;

  return (
    <Link
      href={`/atlas/prompts/${encodeURIComponent(p.name)}`}
      className="atlas-row"
    >
      <div className="atlas-row-name">{p.name}</div>
      <div className="atlas-row-desc">
        {recent > 0 ? (
          <div
            style={{
              position: "relative",
              height: "0.55rem",
              width: "100%",
              maxWidth: "16rem",
              background: "var(--a-line)",
              borderRadius: "2px",
              overflow: "hidden",
            }}
            title={`${recent} recent exchanges where this prompt's call type fired`}
          >
            <div
              style={{
                position: "absolute",
                inset: 0,
                width: `${widthPct}%`,
                background: "var(--a-accent)",
                opacity: 0.7,
              }}
            />
          </div>
        ) : (
          <span
            style={{
              color: "var(--a-muted)",
              fontFamily: "var(--a-mono)",
              fontSize: "0.74rem",
            }}
          >
            no recent invocations
          </span>
        )}
      </div>
      <div className="atlas-row-meta">
        <span
          className={`atlas-chip ${recent > 0 ? "is-accent" : "is-muted"}`}
          title="recent_invocations: count of recent exchanges whose call type's composition references this file"
        >
          {recent}× recent
        </span>
        <span
          className="atlas-chip is-muted"
          title="n_compositions: call-type compositions that reference this file"
        >
          {comps} comp{comps === 1 ? "" : "s"}
        </span>
        <span className="atlas-chip is-muted" title="number of ## sections">
          {sections} sec{sections === 1 ? "" : "s"}
        </span>
        <span
          className="atlas-chip is-muted"
          title={`${p.char_count} chars`}
        >
          {(p.char_count / 1000).toFixed(1)}k
        </span>
      </div>
    </Link>
  );
}
