import Link from "next/link";
import type { SearchHit, SearchResults } from "@/api";
import { atlasFetch } from "../_lib/fetch";
import { Crumbs } from "../_components/Crumbs";

export const metadata = { title: "search" };

const KIND_ORDER = [
  "workflow",
  "call",
  "stage",
  "move",
  "dispatch",
  "page",
  "prompt_section",
];

const KIND_LABELS: Record<string, string> = {
  workflow: "workflows",
  call: "call types",
  stage: "stages",
  move: "moves",
  dispatch: "dispatches",
  page: "page types",
  prompt_section: "prompt sections",
};

export default async function SearchPage({
  searchParams,
}: {
  searchParams: Promise<{ q?: string; limit?: string }>;
}) {
  const { q, limit } = await searchParams;
  const query = (q ?? "").trim();
  const lim = limit ? Number(limit) : 50;

  const results: SearchResults | null = query
    ? await atlasFetch<SearchResults | null>(
        `/api/atlas/search?q=${encodeURIComponent(query)}&limit=${lim}`,
        null,
      )
    : null;

  const hits = results?.hits ?? [];
  const grouped = new Map<string, SearchHit[]>();
  for (const h of hits) {
    if (!grouped.has(h.kind)) grouped.set(h.kind, []);
    grouped.get(h.kind)!.push(h);
  }
  const orderedKinds = [
    ...KIND_ORDER.filter((k) => grouped.has(k)),
    ...Array.from(grouped.keys()).filter((k) => !KIND_ORDER.includes(k)),
  ];

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs items={[{ label: "atlas", href: "/atlas" }, { label: "search" }]} />
          <h1 className="is-sans">search</h1>
          <p className="atlas-lede">
            Lexical search across the registry — workflows, call types, stages,
            moves, dispatches, page types, and prompt sections. Press{" "}
            <span className="atlas-mono" style={{ background: "var(--a-line)", padding: "0.05em 0.35em", borderRadius: 2 }}>/</span>{" "}
            anywhere to focus the search bar.
          </p>
        </div>
      </div>

      {!query ? (
        <div className="atlas-empty">
          <strong>type a query</strong>
          example: <Link href="/atlas/search?q=robustness">robustness</Link>,{" "}
          <Link href="/atlas/search?q=consideration">consideration</Link>,{" "}
          <Link href="/atlas/search?q=dispatch">dispatch</Link>.
        </div>
      ) : !results ? (
        <div className="atlas-empty">
          <strong>search unavailable</strong>
          the search endpoint did not respond.
        </div>
      ) : hits.length === 0 ? (
        <div className="atlas-empty">
          <strong>no hits for &quot;{query}&quot;</strong>
          try a different term, or browse the index.
        </div>
      ) : (
        <>
          <div className="atlas-callout">
            <strong>{results.total}</strong> hit{results.total === 1 ? "" : "s"} for{" "}
            <span className="atlas-mono">&quot;{query}&quot;</span>
            {Object.keys(results.by_kind ?? {}).length > 0 && (
              <span style={{ color: "var(--a-fg-soft)" }}>
                {" "}—{" "}
                {Object.entries(results.by_kind ?? {})
                  .sort((a, b) => b[1] - a[1])
                  .map(([k, n]) => `${KIND_LABELS[k] ?? k}: ${n}`)
                  .join(" · ")}
              </span>
            )}
          </div>

          {orderedKinds.map((k) => {
            const list = grouped.get(k) ?? [];
            return (
              <section key={k} className="atlas-search-section">
                <div className="atlas-search-section-head">
                  <h3>{KIND_LABELS[k] ?? k}</h3>
                  <span className="atlas-section-meta">{list.length}</span>
                </div>
                <div className="atlas-rows" style={{ border: "1px solid var(--a-line)" }}>
                  {list.map((h, i) => (
                    <SearchHitRow key={`${h.kind}-${h.id}-${i}`} hit={h} />
                  ))}
                </div>
              </section>
            );
          })}
        </>
      )}
    </div>
  );
}

function SearchHitRow({ hit }: { hit: SearchHit }) {
  const Body = (
    <>
      <span className="atlas-search-hit-title">{hit.title || hit.id}</span>
      <span className="atlas-search-hit-snippet">
        {hit.snippet || (
          <span style={{ color: "var(--a-muted)", fontStyle: "italic" }}>
            no snippet
          </span>
        )}
      </span>
      <span className="atlas-search-hit-meta">
        <span className="atlas-chip is-muted" style={{ fontSize: "0.62rem" }}>
          {hit.kind.replace("_", " ")}
        </span>
        {hit.score != null && <span style={{ marginLeft: "0.5rem" }}>{hit.score.toFixed(0)}</span>}
      </span>
    </>
  );
  if (hit.href) {
    return (
      <Link href={hit.href} className="atlas-search-hit">
        {Body}
      </Link>
    );
  }
  return <div className="atlas-search-hit">{Body}</div>;
}
