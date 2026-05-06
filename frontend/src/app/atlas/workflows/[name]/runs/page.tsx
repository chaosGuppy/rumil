import Link from "next/link";
import type { RunRollup, RunOutcome } from "@/api";
import { atlasFetch } from "../../../_lib/fetch";
import { Crumbs } from "../../../_components/Crumbs";
import { fmtCost, fmtDuration, fmtWhen } from "../../../_lib/format";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ name: string }>;
}) {
  const { name } = await params;
  return { title: `${name} · runs` };
}

type SortKey = "recent" | "cost" | "duration" | "calls";
const SORT_KEYS: SortKey[] = ["recent", "cost", "duration", "calls"];

function outcomeChipClass(label: string): string {
  switch ((label || "").toLowerCase()) {
    case "complete":
      return "is-outcome-complete";
    case "noop":
      return "is-outcome-noop";
    case "error":
    case "failed":
    case "aborted":
      return "is-outcome-error";
    case "external":
      return "is-outcome-external";
    default:
      return "is-muted";
  }
}

function deriveOutcome(r: RunRollup): RunOutcome {
  if (r.outcome) return r.outcome;
  if (r.is_noop) return { label: "noop", source: "derived" };
  const status = r.last_status;
  if (status === "complete") return { label: "complete", source: "derived" };
  if (status === "error") return { label: "error", source: "derived" };
  return { label: status ?? "—", source: "derived" };
}

function buildHref(
  name: string,
  params: Record<string, string | undefined>,
): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v != null && v !== "") qs.set(k, v);
  }
  const s = qs.toString();
  return `/atlas/workflows/${encodeURIComponent(name)}/runs${s ? `?${s}` : ""}`;
}

export default async function WorkflowRunsList({
  params,
  searchParams,
}: {
  params: Promise<{ name: string }>;
  searchParams: Promise<{
    project_id?: string;
    order_by?: string;
    limit?: string;
    include_noop?: string;
  }>;
}) {
  const { name } = await params;
  const sp = await searchParams;
  const projectId = sp.project_id;
  const orderBy = (SORT_KEYS as string[]).includes(sp.order_by ?? "")
    ? (sp.order_by as SortKey)
    : "recent";
  const limit = sp.limit ?? "100";
  const includeNoop = sp.include_noop !== "false";

  const qs = new URLSearchParams();
  qs.set("order_by", orderBy);
  qs.set("limit", limit);
  qs.set("include_noop", includeNoop ? "true" : "false");
  if (projectId) qs.set("project_id", projectId);

  const runs = await atlasFetch<RunRollup[]>(
    `/api/atlas/workflows/${encodeURIComponent(name)}/runs?${qs.toString()}`,
    [],
  );

  const baseQs = { project_id: projectId, limit };

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs
            items={[
              { label: "atlas", href: "/atlas" },
              { label: "workflows", href: "/atlas/workflows" },
              { label: name, href: `/atlas/workflows/${name}` },
              { label: "runs" },
            ]}
          />
          <h1>{name} · runs</h1>
          <p className="atlas-lede">
            Every run that landed against{" "}
            <span className="atlas-mono">{name}</span> — sortable, filterable,
            and threaded through to flow / overlay / live views.
          </p>
        </div>
      </div>

      <div className="atlas-toolbar">
        <span className="atlas-section-meta">sort</span>
        <div className="atlas-sort-tabs" role="tablist">
          {SORT_KEYS.map((k) => (
            <Link
              key={k}
              role="tab"
              aria-selected={orderBy === k}
              href={buildHref(name, {
                ...baseQs,
                order_by: k,
                include_noop: includeNoop ? undefined : "false",
              })}
              className={`atlas-sort-tab ${orderBy === k ? "is-active" : ""}`}
            >
              {k}
            </Link>
          ))}
        </div>
        <Link
          href={buildHref(name, {
            ...baseQs,
            order_by: orderBy,
            include_noop: includeNoop ? "false" : undefined,
          })}
          className={`atlas-toggle-link ${!includeNoop ? "is-active" : ""}`}
        >
          {!includeNoop ? "× hide noops" : "+ hide noops"}
        </Link>
        <Link
          href={`/atlas/workflows/${encodeURIComponent(name)}/aggregate${projectId ? `?project_id=${projectId}` : ""}`}
          className="atlas-toggle-link"
        >
          ← aggregate
        </Link>
        <span className="atlas-section-meta" style={{ marginLeft: "auto" }}>
          {runs.length} run{runs.length === 1 ? "" : "s"}
        </span>
      </div>

      {runs.length === 0 ? (
        <div className="atlas-empty">
          <strong>no runs</strong>
          {!includeNoop && "noops are hidden — toggle them on to widen the set."}
        </div>
      ) : (
        <>
          <div
            className="atlas-run-row"
            style={{ background: "var(--a-bg)", color: "var(--a-muted)", fontSize: "0.6rem", textTransform: "uppercase", letterSpacing: "0.14em", borderBottom: "1px solid var(--a-line-strong)" }}
          >
            <span>when</span>
            <span>headline</span>
            <span style={{ textAlign: "right" }}>cost</span>
            <span style={{ textAlign: "right" }}>calls</span>
            <span style={{ textAlign: "right" }}>disp</span>
            <span style={{ textAlign: "right" }}>dur</span>
            <span style={{ textAlign: "right" }}>j/v/q</span>
            <span style={{ textAlign: "right" }}>outcome</span>
          </div>
          <div className="atlas-run-list">
            {runs.map((r) => {
              const outcome = deriveOutcome(r);
              const headline =
                r.question_headline ??
                r.name ??
                r.run_id.slice(0, 8);
              const j = r.n_judgements_created ?? 0;
              const v = r.n_views_created ?? 0;
              const q = r.n_questions_created ?? 0;
              return (
                <Link
                  key={r.run_id}
                  href={`/atlas/runs/${r.run_id}/flow`}
                  className="atlas-run-row"
                >
                  <span className="atlas-run-row-when">{fmtWhen(r.created_at)}</span>
                  <span className="atlas-run-row-headline">
                    <span>{headline}</span>
                    <span className="atlas-id">{r.run_id.slice(0, 8)}</span>
                  </span>
                  <span className={`atlas-run-row-num ${(r.cost_usd ?? 0) === 0 ? "is-zero" : ""}`}>
                    {fmtCost(r.cost_usd ?? 0)}
                  </span>
                  <span className={`atlas-run-row-num ${(r.n_calls ?? 0) === 0 ? "is-zero" : ""}`}>
                    {r.n_calls ?? 0}
                  </span>
                  <span className={`atlas-run-row-num ${(r.n_dispatches ?? 0) === 0 ? "is-zero" : ""}`}>
                    {r.n_dispatches ?? 0}
                  </span>
                  <span className="atlas-run-row-num">{fmtDuration(r.duration_seconds)}</span>
                  <span
                    className={`atlas-run-row-num ${j + v + q === 0 ? "is-zero" : ""}`}
                    title={`${j} judgements · ${v} views · ${q} questions created`}
                  >
                    {j}/{v}/{q}
                  </span>
                  <span className="atlas-run-row-trail">
                    <span
                      className={`atlas-chip ${outcomeChipClass(outcome.label)}`}
                      title={outcome.detail || outcome.source}
                    >
                      {outcome.label}
                      {outcome.score != null && (
                        <span style={{ marginLeft: "0.25rem", color: "var(--a-muted)" }}>
                          {outcome.score.toFixed(2)}
                        </span>
                      )}
                    </span>
                  </span>
                </Link>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
