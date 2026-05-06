import Link from "next/link";
import type { ExchangeSearchResults } from "@/api";
import { atlasFetch } from "../../_lib/fetch";
import { fmtRelative } from "../../_lib/format";
import { Crumbs } from "../../_components/Crumbs";

export const metadata = { title: "exchange search" };

export default async function ExchangeSearchPage({
  searchParams,
}: {
  searchParams: Promise<{ q?: string; limit?: string; scan?: string; call_type?: string }>;
}) {
  const { q, limit, scan, call_type } = await searchParams;
  const query = (q ?? "").trim();
  const lim = limit ? Number(limit) : 50;
  const scn = scan ? Number(scan) : 200;

  const qs = new URLSearchParams();
  if (query) qs.set("q", query);
  qs.set("limit", String(lim));
  qs.set("scan", String(scn));
  if (call_type) qs.set("call_type", call_type);

  const results: ExchangeSearchResults | null = query
    ? await atlasFetch<ExchangeSearchResults | null>(
        `/api/atlas/exchanges/search?${qs.toString()}`,
        null,
      )
    : null;

  const hits = results?.hits ?? [];

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs
            items={[
              { label: "atlas", href: "/atlas" },
              { label: "exchanges" },
              { label: "search" },
            ]}
          />
          <h1 className="is-sans">exchange search</h1>
          <p className="atlas-lede">
            Substring search across captured LLM exchanges (system prompt, user
            messages, response). Each hit links into the playground so you can
            edit the prompt and re-fire.
          </p>
          <form className="atlas-pg-search-form" action="/atlas/exchanges/search">
            <input
              type="text"
              name="q"
              defaultValue={query}
              placeholder="search exchange text…"
              autoFocus
            />
            <input
              type="text"
              name="call_type"
              defaultValue={call_type ?? ""}
              placeholder="call_type filter (optional)"
            />
            <input type="number" name="scan" defaultValue={scn} min={1} max={2000} title="scan" />
            <input type="number" name="limit" defaultValue={lim} min={1} max={500} title="limit" />
            <button type="submit">search</button>
          </form>
        </div>
      </div>

      {!query ? (
        <div className="atlas-empty">
          <strong>type a query</strong>
          example:{" "}
          <Link href="/atlas/exchanges/search?q=robustness&scan=200">
            robustness
          </Link>
          ,{" "}
          <Link href="/atlas/exchanges/search?q=consideration&scan=200">
            consideration
          </Link>
          .
        </div>
      ) : !results ? (
        <div className="atlas-empty">
          <strong>search unavailable</strong>
          the exchange search endpoint did not respond.
        </div>
      ) : hits.length === 0 ? (
        <div className="atlas-empty">
          <strong>no hits for &quot;{query}&quot;</strong>
          scanned {results.n_scanned} exchange{results.n_scanned === 1 ? "" : "s"}.
          Try widening scan, dropping the call_type filter, or a different term.
        </div>
      ) : (
        <>
          <div className="atlas-callout">
            <strong>{results.total}</strong> hit{results.total === 1 ? "" : "s"}{" "}
            (scanned {results.n_scanned})
            {results.truncated && (
              <span style={{ color: "var(--a-warm)", marginLeft: "0.5rem" }}>
                · truncated
              </span>
            )}
          </div>
          <div className="atlas-rows" style={{ border: "1px solid var(--a-line)" }}>
            {hits.map((h, i) => (
              <Link
                key={`${h.exchange_id}-${i}`}
                href={`/atlas/exchanges/${encodeURIComponent(h.exchange_id)}/playground`}
                className="atlas-search-hit"
              >
                <span className="atlas-search-hit-title">
                  {h.call_type}
                  <span style={{ color: "var(--a-muted)", marginLeft: 8, fontSize: "0.7rem" }}>
                    {h.exchange_id.slice(0, 8)}
                  </span>
                </span>
                <span className="atlas-search-hit-snippet">
                  <span className="atlas-mono" style={{ color: "var(--a-muted)", fontSize: "0.66rem", marginRight: 6 }}>
                    {h.field}
                  </span>
                  {h.snippet}
                </span>
                <span className="atlas-search-hit-meta">
                  {h.created_at && (
                    <span style={{ color: "var(--a-muted)" }}>
                      {fmtRelative(h.created_at)}
                    </span>
                  )}
                  {h.score != null && (
                    <span style={{ marginLeft: "0.5rem" }}>
                      {h.score.toFixed(0)}
                    </span>
                  )}
                </span>
              </Link>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
