import Link from "next/link";
import type { RecentWorkFeed, RecentWorkItem } from "@/api";
import { atlasFetch } from "../_lib/fetch";
import { Crumbs } from "../_components/Crumbs";
import { CrossLink } from "../_components/CrossLink";

export const metadata = { title: "recent work" };

const PAGE_TYPE_FILTERS = [
  { label: "all", value: "judgement,claim,view" },
  { label: "judgements", value: "judgement" },
  { label: "claims", value: "claim" },
  { label: "views", value: "view" },
] as const;

function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso.slice(0, 10);
  const ms = Date.now() - t;
  const m = Math.floor(ms / 60_000);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  const mo = Math.floor(d / 30);
  return `${mo}mo ago`;
}

export default async function RecentWorkPage({
  searchParams,
}: {
  searchParams: Promise<{ project_id?: string; workflow?: string; types?: string }>;
}) {
  const sp = await searchParams;
  const projectId = sp.project_id;
  const workflow = sp.workflow;
  const types = sp.types || "judgement,claim,view";

  const qs = new URLSearchParams();
  if (projectId) qs.set("project_id", projectId);
  if (workflow) qs.set("workflow_name", workflow);
  qs.set("page_types", types);
  qs.set("limit", "60");

  const feed = await atlasFetch<RecentWorkFeed>(
    `/api/atlas/feed/recent_work?${qs.toString()}`,
    { items: [], n_items: 0, filters_applied: {} },
  );

  const items = feed.items ?? [];

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs items={[{ label: "atlas", href: "/atlas" }, { label: "recent work" }]} />
          <h1 className="is-sans">recent work</h1>
          <p className="atlas-lede">
            What rumil&apos;s actually been producing — claims, judgements,
            views — with the workflow + run + call that emitted each one.
            Skim to read what kind of work the system has been doing.
          </p>
        </div>
      </div>

      <div className="atlas-stat-grid" style={{ marginBottom: "1rem" }}>
        {PAGE_TYPE_FILTERS.map((f) => {
          const next = new URLSearchParams();
          if (projectId) next.set("project_id", projectId);
          if (workflow) next.set("workflow", workflow);
          next.set("types", f.value);
          const active = types === f.value;
          return (
            <Link
              key={f.value}
              href={`/atlas/feed?${next.toString()}`}
              className="atlas-stat atlas-stat-link"
              style={{
                background: active ? "var(--a-accent-soft)" : "var(--a-bg-paper)",
              }}
            >
              <span className="atlas-stat-label">{f.label}</span>
            </Link>
          );
        })}
      </div>

      {items.length === 0 ? (
        <div className="atlas-empty">
          <strong>no recent work</strong>
          no claims / judgements / views in the recent window.
        </div>
      ) : (
        <div className="atlas-rows">
          {items.map((it) => (
            <FeedRow key={it.page_id} it={it} />
          ))}
        </div>
      )}
    </div>
  );
}

function FeedRow({ it }: { it: RecentWorkItem }) {
  const typeColor =
    it.page_type === "judgement"
      ? "is-success"
      : it.page_type === "claim"
        ? "is-orchestrator"
        : it.page_type === "view"
          ? "is-versus"
          : "is-muted";
  const cred = it.credence;
  const rob = it.robustness;
  return (
    <article className="atlas-feed-row">
      <div className="atlas-feed-row-head">
        <span className={`atlas-chip ${typeColor}`}>{it.page_type}</span>
        {cred != null && (
          <span className="atlas-chip is-muted" title="credence">
            cred {cred}/9
          </span>
        )}
        {rob != null && (
          <span className="atlas-chip is-muted" title="robustness">
            rob {rob}/5
          </span>
        )}
        <span className="atlas-feed-row-time" title={it.created_at}>
          {fmtRelative(it.created_at)}
        </span>
        {it.workflow_name && (
          <Link
            href={`/atlas/workflows/${encodeURIComponent(it.workflow_name)}`}
            className="atlas-chip is-orchestrator"
          >
            {it.workflow_name}
          </Link>
        )}
        {it.call_type && (
          <Link
            href={`/atlas/calls/${encodeURIComponent(it.call_type)}`}
            className="atlas-chip"
          >
            {it.call_type}
          </Link>
        )}
        {it.superseded && (
          <span className="atlas-chip is-warm" title="this page has been superseded">
            superseded
          </span>
        )}
      </div>
      <div className="atlas-feed-row-headline">{it.headline || "(no headline)"}</div>
      {it.abstract && <div className="atlas-feed-row-abstract">{it.abstract}</div>}
      {it.content_preview && !it.abstract && (
        <div className="atlas-feed-row-preview">{it.content_preview}</div>
      )}
      {it.credence_reasoning && (
        <div className="atlas-feed-row-reasoning">
          <span style={{ color: "var(--a-muted)", marginRight: "0.4rem" }}>why:</span>
          {it.credence_reasoning}
        </div>
      )}
      <div className="atlas-feed-row-foot">
        <CrossLink to={`/pages/${encodeURIComponent(it.page_id)}`}>
          page {it.page_id.slice(0, 8)}
        </CrossLink>
        {it.run_id && (
          <Link href={`/atlas/runs/${encodeURIComponent(it.run_id)}/flow`}>
            run {it.run_id.slice(0, 8)} →
          </Link>
        )}
        {it.project_name && (
          <span className="atlas-feed-row-project">{it.project_name}</span>
        )}
      </div>
    </article>
  );
}
