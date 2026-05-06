import Link from "next/link";
import type { PageTypeSummary } from "@/api";
import { atlasFetch } from "../_lib/fetch";
import { Crumbs } from "../_components/Crumbs";
import { IntensityBar } from "../_components/IntensityBar";

export const metadata = { title: "page types" };

export default async function PageTypesList() {
  const types = await atlasFetch<PageTypeSummary[]>(
    "/api/atlas/registry/pages",
    [],
  );
  const sorted = types.slice().sort(
    (a, b) =>
      (b.lifetime_count ?? 0) - (a.lifetime_count ?? 0) ||
      a.page_type.localeCompare(b.page_type),
  );
  const maxLifetime = Math.max(1, ...sorted.map((t) => t.lifetime_count ?? 0));

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
        {sorted.map((t) => {
          const lifetime = t.lifetime_count ?? 0;
          return (
            <Link
              key={t.page_type}
              href={`/atlas/pages/${encodeURIComponent(t.page_type)}`}
              className="atlas-row"
            >
              <div className="atlas-row-name">{t.page_type}</div>
              <div className="atlas-row-desc">
                <div style={{ marginBottom: "0.4rem" }}>{t.description}</div>
                <IntensityBar
                  lifetime={lifetime}
                  lifetimeMax={maxLifetime}
                  lifetimeLabel="lifetime pages of this type"
                />
              </div>
              <div className="atlas-row-meta">
                <span
                  className="atlas-chip is-muted"
                  title="lifetime count of pages with this page_type"
                >
                  {lifetime}× lifetime
                </span>
                {t.layer_hint && (
                  <span className="atlas-chip is-muted">{t.layer_hint}</span>
                )}
              </div>
            </Link>
          );
        })}
      </div>
    </div>
  );
}
