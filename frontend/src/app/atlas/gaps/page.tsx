import Link from "next/link";
import type { GapItem, GapsReport } from "@/api";
import { atlasFetch } from "../_lib/fetch";
import { Crumbs } from "../_components/Crumbs";

export const metadata = { title: "gaps" };

const KIND_LABELS: Record<string, string> = {
  orphan_prompt_file: "orphan prompt",
  move_in_no_preset: "unrouted move",
};

const KIND_GO_LABELS: Record<string, string> = {
  orphan_prompt_file: "open prompt →",
  move_in_no_preset: "open move →",
  call_type_without_runner: "open call →",
  dispatchable_without_dispatch_def: "open call →",
  call_type_without_description: "open call →",
  page_type_without_description: "open page →",
  workflow_setting_missing: "open workflow →",
  workflow_code_path_missing: "open workflow →",
};

function kindClass(kind: string): string {
  if (kind.startsWith("orphan_")) return "is-orphan";
  return "";
}

function goLabel(kind: string): string {
  return KIND_GO_LABELS[kind] ?? "open →";
}

export default async function GapsPage({
  searchParams,
}: {
  searchParams: Promise<{ kind?: string }>;
}) {
  const { kind } = await searchParams;
  const report = await atlasFetch<GapsReport>("/api/atlas/gaps", {
    items: [],
    counts_by_kind: {},
  });

  const items = report.items ?? [];
  const counts = report.counts_by_kind ?? {};
  const kinds = Object.keys(counts).sort();
  const filtered = kind ? items.filter((it) => it.kind === kind) : items;

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs items={[{ label: "atlas", href: "/atlas" }, { label: "gaps" }]} />
          <h1 className="is-sans">gaps</h1>
          <p className="atlas-lede">
            Inconsistencies the registry knows about — files that no composition
            references, moves that no preset includes, and similar dangling
            edges. The list is generated from the registry on every load.
          </p>
        </div>
      </div>

      <div className="atlas-stat-grid" style={{ marginBottom: "1.5rem" }}>
        <Link
          href="/atlas/gaps"
          className="atlas-stat atlas-stat-link"
          style={{ background: kind ? "var(--a-bg-paper)" : "var(--a-accent-soft)" }}
        >
          <span className="atlas-stat-num">{items.length}</span>
          <span className="atlas-stat-label">all gaps</span>
        </Link>
        {kinds.map((k) => (
          <Link
            key={k}
            href={`/atlas/gaps?kind=${encodeURIComponent(k)}`}
            className="atlas-stat atlas-stat-link"
            style={{
              background: kind === k ? "var(--a-accent-soft)" : "var(--a-bg-paper)",
            }}
          >
            <span className="atlas-stat-num">{counts[k]}</span>
            <span className="atlas-stat-label">{KIND_LABELS[k] ?? k.replace(/_/g, " ")}</span>
          </Link>
        ))}
      </div>

      {filtered.length === 0 ? (
        <div className="atlas-empty">
          <strong>no gaps</strong>
          {kind
            ? `nothing under "${kind}" — try clearing the filter.`
            : "the registry is internally consistent."}
        </div>
      ) : (
        <div className="atlas-rows">
          {filtered.map((it, i) => (
            <GapRow key={`${it.kind}-${it.target}-${i}`} item={it} />
          ))}
        </div>
      )}
    </div>
  );
}

function GapRow({ item }: { item: GapItem }) {
  const label = KIND_LABELS[item.kind] ?? item.kind.replace(/_/g, " ");
  return (
    <div className="atlas-gap-row">
      <span className={`atlas-gap-kind ${kindClass(item.kind)}`}>{label}</span>
      <span className="atlas-gap-target">{item.target}</span>
      <span className="atlas-gap-detail">{item.detail || ""}</span>
      {item.href ? (
        <Link href={item.href} className="atlas-count-link">
          {goLabel(item.kind)}
        </Link>
      ) : (
        <span style={{ color: "var(--a-muted)", fontSize: "0.7rem" }}>—</span>
      )}
    </div>
  );
}
