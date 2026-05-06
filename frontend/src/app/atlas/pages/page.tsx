import Link from "next/link";
import type { PageTypeSummary } from "@/api";
import { atlasFetch } from "../_lib/fetch";
import { Crumbs } from "../_components/Crumbs";

export const metadata = { title: "page types" };

export default async function PageTypesList() {
  const types = await atlasFetch<PageTypeSummary[]>(
    "/api/atlas/registry/pages",
    [],
  );

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs items={[{ label: "atlas", href: "/atlas" }, { label: "pages" }]} />
          <h1 className="is-sans">page types</h1>
          <p className="atlas-lede">
            The typed nodes in the research graph. Every move that mutates the
            workspace creates or links pages of these types; the cross-reference
            below shows which moves create each.
          </p>
        </div>
      </div>

      <div className="atlas-rows">
        {types.map((t) => (
          <Link
            key={t.page_type}
            href={`/atlas/pages/${encodeURIComponent(t.page_type)}`}
            className="atlas-row"
          >
            <div className="atlas-row-name">{t.page_type}</div>
            <div className="atlas-row-desc">{t.description}</div>
            <div className="atlas-row-meta">
              {t.layer_hint && (
                <span className="atlas-chip is-muted">{t.layer_hint}</span>
              )}
            </div>
          </Link>
        ))}
      </div>
    </div>
  );
}
