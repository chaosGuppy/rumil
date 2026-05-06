import Link from "next/link";
import type { ErrorIndex, ErrorListItem } from "@/api";
import { atlasFetch } from "../_lib/fetch";
import { fmtRelative, fmtWhen } from "../_lib/format";
import { Crumbs } from "../_components/Crumbs";

export const metadata = { title: "errors" };

export default async function ErrorsPage({
  searchParams,
}: {
  searchParams: Promise<{ project_id?: string; call_type?: string }>;
}) {
  const sp = await searchParams;
  const qs = new URLSearchParams();
  if (sp.project_id) qs.set("project_id", sp.project_id);
  if (sp.call_type) qs.set("call_type", sp.call_type);
  qs.set("limit", "100");
  qs.set("scan", "1000");
  const idx = await atlasFetch<ErrorIndex>(
    `/api/atlas/errors?${qs.toString()}`,
    { items: [], n_scanned: 0, truncated: false },
  );
  const items = idx.items ?? [];
  const nScanned = idx.n_scanned ?? 0;

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs items={[{ label: "atlas", href: "/atlas" }, { label: "errors" }]} />
          <h1 className="is-sans">errors</h1>
          <div className="atlas-chip-row" style={{ marginBottom: "0.85rem" }}>
            <span className="atlas-chip is-muted">
              {items.length} surfaced · {nScanned} scanned
            </span>
            {sp.call_type && (
              <Link href="/atlas/errors" className="atlas-chip is-accent">
                clear call_type filter
              </Link>
            )}
            {sp.project_id && (
              <Link href="/atlas/errors" className="atlas-chip is-accent">
                clear project_id filter
              </Link>
            )}
          </div>
        </div>
      </div>

      {items.length === 0 ? (
        <div className="atlas-empty">
          <strong>no recent errors</strong>
          {nScanned > 0
            ? `scanned ${nScanned} exchanges; none had a non-empty error.`
            : "no exchanges in scan window."}
        </div>
      ) : (
        <div className="atlas-rows">
          {items.map((e) => (
            <ErrorRow key={e.exchange_id} e={e} />
          ))}
        </div>
      )}
    </div>
  );
}

function ErrorRow({ e }: { e: ErrorListItem }) {
  return (
    <div
      className="atlas-row"
      style={{
        gridTemplateColumns: "minmax(7rem, 9rem) minmax(8rem, 12rem) 1fr auto",
        background: "var(--a-warm-soft)",
      }}
    >
      <div
        className="atlas-row-name"
        title={e.created_at ? fmtWhen(e.created_at) : ""}
        style={{ fontWeight: 400 }}
      >
        {fmtRelative(e.created_at)}
      </div>
      <div className="atlas-row-name" style={{ fontSize: "0.74rem" }}>
        {e.call_type ? (
          <Link
            href={`/atlas/calls/${encodeURIComponent(e.call_type)}`}
            style={{ color: "inherit" }}
          >
            {e.call_type}
          </Link>
        ) : (
          <span style={{ color: "var(--a-muted)" }}>—</span>
        )}
        {e.project_name && (
          <div
            style={{
              fontFamily: "var(--a-sans)",
              fontSize: "0.7rem",
              color: "var(--a-muted)",
              fontWeight: 400,
              marginTop: "0.15rem",
            }}
            title={`project_id ${e.project_id}`}
          >
            {e.project_name}
          </div>
        )}
      </div>
      <div
        className="atlas-row-desc"
        style={{
          color: "var(--a-warm)",
          fontFamily: "var(--a-mono)",
          fontSize: "0.78rem",
          WebkitLineClamp: 3,
        }}
      >
        {e.message || "(no message)"}
      </div>
      <div className="atlas-row-meta">
        <Link
          href={`/atlas/exchanges/${encodeURIComponent(e.exchange_id)}/playground`}
          className="atlas-chip"
          title="open this exchange in the playground"
        >
          ex {e.exchange_id.slice(0, 8)}
        </Link>
        <Link
          href={`/atlas/calls/by_id/${encodeURIComponent(e.call_id)}/exchanges#exch-${encodeURIComponent(e.exchange_id)}`}
          className="atlas-chip"
          title="every exchange recorded against this call"
        >
          call {e.call_id.slice(0, 8)}
        </Link>
        {e.run_id && (
          <Link
            href={`/atlas/runs/${encodeURIComponent(e.run_id)}/flow`}
            className="atlas-chip"
            title="run flow"
          >
            run {e.run_id.slice(0, 8)}
          </Link>
        )}
      </div>
    </div>
  );
}
