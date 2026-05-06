import Link from "next/link";
import type { NoveltyItem, NoveltyReport } from "@/api";
import { atlasFetch } from "../_lib/fetch";
import { Crumbs } from "../_components/Crumbs";

export const metadata = { title: "novelty" };

const KIND_LABELS: Record<string, string> = {
  unknown_tool_use: "unknown tool",
  unknown_trace_event: "unknown event",
  unknown_call_type: "unknown call_type",
  orphan_rendered_prompt: "orphan rendered prompt",
};

const KIND_BLURBS: Record<string, string> = {
  unknown_tool_use:
    "Tool name in a real exchange that doesn't match any registered DispatchDef or MoveDef. Either a new tool atlas hasn't seen, or a typo.",
  unknown_trace_event:
    "Trace event type in trace_json that isn't on the TraceEvent discriminated union. Atlas isn't reading this event yet.",
  unknown_call_type:
    "calls.call_type value not on the CallType enum. The FK should prevent this — investigate if seen.",
  orphan_rendered_prompt:
    "system_prompt prefix doesn't match any prompts/*.md file — likely a build_system_prompt bypass path.",
};

function kindClass(kind: string): string {
  if (kind === "orphan_rendered_prompt") return "is-orphan";
  if (kind === "unknown_call_type") return "is-warm";
  return "";
}

export default async function NoveltyPage({
  searchParams,
}: {
  searchParams: Promise<{ kind?: string; project_id?: string }>;
}) {
  const { kind, project_id } = await searchParams;
  const qs = new URLSearchParams();
  if (project_id) qs.set("project_id", project_id);
  const url = qs.toString()
    ? `/api/atlas/novelty?${qs.toString()}`
    : "/api/atlas/novelty";

  const report = await atlasFetch<NoveltyReport>(url, {
    items: [],
    counts_by_kind: {},
    n_scanned_exchanges: 0,
    n_scanned_calls: 0,
  });

  const items = report.items ?? [];
  const counts = report.counts_by_kind ?? {};
  const kinds = Object.keys(counts).sort();
  const filtered = kind ? items.filter((it) => it.kind === kind) : items;

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs items={[{ label: "atlas", href: "/atlas" }, { label: "novelty" }]} />
          <h1 className="is-sans">novelty</h1>
          <p className="atlas-lede">
            What atlas observed in real run/call/exchange data that doesn&apos;t
            match its static registries — unknown tool names, trace event
            kinds outside the TraceEvent union, system prompts that don&apos;t
            match any <code>prompts/*.md</code>, call_type values not on the
            enum. The atlas-noticing-its-own-blind-spots loop.
          </p>
          <p
            className="atlas-stat-panel-meta"
            style={{ marginTop: "0.4rem" }}
          >
            scanned {report.n_scanned_exchanges} exchanges ·{" "}
            {report.n_scanned_calls} calls
          </p>
        </div>
      </div>

      <div className="atlas-stat-grid" style={{ marginBottom: "1.5rem" }}>
        <Link
          href={
            project_id ? `/atlas/novelty?project_id=${project_id}` : "/atlas/novelty"
          }
          className="atlas-stat atlas-stat-link"
          style={{ background: kind ? "var(--a-bg-paper)" : "var(--a-accent-soft)" }}
        >
          <span className="atlas-stat-num">{items.length}</span>
          <span className="atlas-stat-label">all novelty</span>
        </Link>
        {kinds.map((k) => (
          <Link
            key={k}
            href={`/atlas/novelty?kind=${encodeURIComponent(k)}${
              project_id ? `&project_id=${project_id}` : ""
            }`}
            className="atlas-stat atlas-stat-link"
            style={{
              background: kind === k ? "var(--a-accent-soft)" : "var(--a-bg-paper)",
            }}
          >
            <span className="atlas-stat-num">{counts[k]}</span>
            <span className="atlas-stat-label">
              {KIND_LABELS[k] ?? k.replace(/_/g, " ")}
            </span>
          </Link>
        ))}
      </div>

      {kind && KIND_BLURBS[kind] && (
        <div
          className="atlas-callout"
          style={{ marginBottom: "1rem" }}
        >
          {KIND_BLURBS[kind]}
        </div>
      )}

      {filtered.length === 0 ? (
        <div className="atlas-empty">
          <strong>nothing novel</strong>
          {kind
            ? `nothing under "${kind}" in the recent scan window.`
            : "real data matches atlas's static registries cleanly."}
        </div>
      ) : (
        <div className="atlas-rows">
          {filtered.map((it, i) => (
            <NoveltyRow
              key={`${it.kind}-${it.target}-${i}`}
              item={it}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function NoveltyRow({ item }: { item: NoveltyItem }) {
  const label = KIND_LABELS[item.kind] ?? item.kind.replace(/_/g, " ");
  return (
    <div className="atlas-gap-row">
      <span className={`atlas-gap-kind ${kindClass(item.kind)}`}>{label}</span>
      <span className="atlas-gap-target">
        {item.target}
        {(item.seen_count ?? 0) > 1 && (
          <span style={{ color: "var(--a-muted)", marginLeft: "0.5rem" }}>
            × {item.seen_count}
          </span>
        )}
      </span>
      <span className="atlas-gap-detail">{item.detail || ""}</span>
      <div style={{ display: "flex", gap: "0.4rem" }}>
        {item.sample_call_id && (
          <Link
            href={`/atlas/calls/by_id/${encodeURIComponent(
              item.sample_call_id,
            )}/exchanges`}
            className="atlas-count-link"
            title="sample call"
          >
            call {item.sample_call_id.slice(0, 8)} →
          </Link>
        )}
        {item.sample_run_id && (
          <Link
            href={`/atlas/runs/${encodeURIComponent(item.sample_run_id)}/flow`}
            className="atlas-count-link"
            title="sample run"
          >
            run {item.sample_run_id.slice(0, 8)} →
          </Link>
        )}
      </div>
    </div>
  );
}
