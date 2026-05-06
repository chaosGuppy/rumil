import Link from "next/link";
import { notFound } from "next/navigation";
import type { MoveSummary, PageTypeSummary } from "@/api";
import { atlasFetch } from "../../_lib/fetch";
import { Crumbs } from "../../_components/Crumbs";
import { isUuidLike } from "../../_lib/format";
import { PageInstanceDetail } from "./PageInstanceDetail";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ page_type: string }>;
}) {
  const { page_type } = await params;
  return { title: `page · ${page_type}` };
}

export default async function PageTypeDetail({
  params,
}: {
  params: Promise<{ page_type: string }>;
}) {
  const { page_type } = await params;

  // The slug doubles as page_id when UUID-shaped — render an instance detail.
  if (isUuidLike(page_type)) {
    return <PageInstanceDetail pageId={page_type} />;
  }

  const [t, allMoves] = await Promise.all([
    atlasFetch<PageTypeSummary | null>(
      `/api/atlas/registry/pages/${encodeURIComponent(page_type)}`,
      null,
    ),
    atlasFetch<MoveSummary[]>("/api/atlas/registry/moves", []),
  ]);
  if (!t) notFound();

  const creators = allMoves.filter(
    (m) => m.name === `create_${page_type}` || m.move_type === `CREATE_${page_type.toUpperCase()}`,
  );
  const linkers = allMoves.filter((m) =>
    m.name.toLowerCase().includes(page_type.toLowerCase()) && !creators.includes(m),
  );

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs
            items={[
              { label: "atlas", href: "/atlas" },
              { label: "pages", href: "/atlas/pages" },
              { label: t.page_type },
            ]}
          />
          <h1>{t.page_type}</h1>
          <div className="atlas-chip-row" style={{ marginBottom: "0.85rem" }}>
            {t.layer_hint && (
              <span className="atlas-chip is-muted">layer · {t.layer_hint}</span>
            )}
            <span className="atlas-chip is-muted">
              {creators.length} creator move{creators.length === 1 ? "" : "s"}
            </span>
          </div>
          <p className="atlas-lede">{t.description}</p>
        </div>
      </div>

      <section className="atlas-section">
        <div className="atlas-section-head">
          <h2>moves that create this type</h2>
          <span className="atlas-section-meta">
            matched on name pattern create_{page_type}
          </span>
        </div>
        {creators.length === 0 ? (
          <div className="atlas-empty">
            <strong>no creator moves found</strong>
            this page type may be created by the system rather than by an LLM
            move.
          </div>
        ) : (
          <div className="atlas-rows">
            {creators.map((m) => (
              <Link
                key={m.move_type}
                href={`/atlas/moves/${encodeURIComponent(m.move_type)}`}
                className="atlas-row"
              >
                <div className="atlas-row-name">{m.name}</div>
                <div className="atlas-row-desc">{m.description}</div>
                <div className="atlas-row-meta">
                  <span className="atlas-chip is-muted">
                    {(m.fields ?? []).length} fields
                  </span>
                </div>
              </Link>
            ))}
          </div>
        )}
      </section>

      {linkers.length > 0 && (
        <section className="atlas-section">
          <div className="atlas-section-head">
            <h2>related moves</h2>
            <span className="atlas-section-meta">
              moves that mention {t.page_type} by name
            </span>
          </div>
          <div className="atlas-chip-row">
            {linkers.map((m) => (
              <Link
                key={m.move_type}
                href={`/atlas/moves/${encodeURIComponent(m.move_type)}`}
                className="atlas-chip"
              >
                {m.name}
              </Link>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
