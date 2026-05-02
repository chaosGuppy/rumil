"use client";

import Link from "next/link";
import type { ContextBuiltEventOut, PageRef } from "@/api/types.gen";
import { useStagedRun } from "@/lib/staged-run-context";
import { withStagedRun } from "@/lib/staged-run-href";

export function PageChip({ page }: { page: PageRef }) {
  const short = page.id.slice(0, 8);
  const label = page.headline || short;
  const { activeStagedRunId } = useStagedRun();
  const href = withStagedRun(`/pages/${page.id}`, activeStagedRunId);
  return (
    <Link href={href} className="trace-page-chip" title={short}>
      {label}
    </Link>
  );
}

export function PageList({ pages }: { pages: PageRef[] }) {
  if (!pages || pages.length === 0)
    return <span className="trace-empty">none</span>;
  return (
    <span className="trace-page-list">
      {pages.map((p, i) => (
        <PageChip key={`${p.id}-${i}`} page={p} />
      ))}
    </span>
  );
}

export type CtxTierKey = "full" | "distillation" | "abstract" | "summary";

export const CTX_TIER_ORDER: { key: CtxTierKey; label: string }[] = [
  { key: "full", label: "full" },
  { key: "distillation", label: "distillation" },
  { key: "abstract", label: "abstract" },
  { key: "summary", label: "summary" },
];

export function ContextBuiltBody({ event }: { event: ContextBuiltEventOut }) {
  const working = event.working_context_page_ids ?? [];
  const preloaded = event.preloaded_page_ids ?? [];
  const scopeLinked = event.scope_linked_pages ?? [];
  const budgetUsage = event.budget_usage ?? {};
  const tierPages: Record<CtxTierKey, PageRef[]> = {
    full: event.full_pages ?? [],
    distillation: event.distillation_pages ?? [],
    abstract: event.abstract_pages ?? [],
    summary: event.summary_pages ?? [],
  };
  const populatedTiers = CTX_TIER_ORDER.filter(
    (t) => tierPages[t.key].length > 0 || (budgetUsage[t.key] ?? 0) > 0,
  );
  const totalTierChars = CTX_TIER_ORDER.reduce(
    (sum, t) => sum + (budgetUsage[t.key] ?? 0),
    0,
  );
  const uniquePageIds = new Set<string>();
  for (const t of populatedTiers) for (const p of tierPages[t.key]) uniquePageIds.add(p.id);
  for (const p of scopeLinked) uniquePageIds.add(p.id);
  for (const p of preloaded) uniquePageIds.add(p.id);
  if (populatedTiers.length === 0) for (const p of working) uniquePageIds.add(p.id);
  const totalPages = uniquePageIds.size;
  const promptChars =
    event.context_text_chars ?? (event.context_text?.length ?? 0);

  return (
    <div className="trace-event-body">
      <div className="trace-ctx-totals">
        <span className="trace-ctx-totals-pages">
          {totalPages} page{totalPages === 1 ? "" : "s"}
        </span>
        {promptChars > 0 && (
          <>
            <span className="trace-ctx-totals-sep">·</span>
            <span className="trace-ctx-totals-chars">
              {promptChars.toLocaleString()} prompt ch
            </span>
          </>
        )}
        {totalTierChars > 0 && totalTierChars !== promptChars && (
          <>
            <span className="trace-ctx-totals-sep">·</span>
            <span className="trace-ctx-totals-chars">
              {totalTierChars.toLocaleString()} tiered ch
            </span>
          </>
        )}
      </div>

      {totalTierChars > 0 && (
        <div
          className="trace-ctx-budget-bar"
          title={`tiered budget: ${totalTierChars.toLocaleString()} chars`}
        >
          {populatedTiers.map((t) => {
            const chars = budgetUsage[t.key] ?? 0;
            if (chars <= 0) return null;
            return (
              <div
                key={t.key}
                className={`trace-ctx-budget-seg trace-ctx-budget-seg--${t.key}`}
                style={{ flexGrow: chars }}
                title={`${t.label}: ${chars.toLocaleString()} ch`}
              />
            );
          })}
        </div>
      )}

      {populatedTiers.map((t) => {
        const pages = tierPages[t.key];
        const chars = budgetUsage[t.key] ?? 0;
        return (
          <div key={t.key} className="trace-ctx-tier">
            <span className="trace-ctx-tier-label">
              <span
                className={`trace-ctx-tier-swatch trace-ctx-tier-swatch--${t.key}`}
              />
              {t.label}
              <span className="trace-ctx-tier-count">({pages.length})</span>
            </span>
            <PageList pages={pages} />
            <span className="trace-ctx-tier-chars">
              {chars > 0 ? `${chars.toLocaleString()} ch` : "—"}
            </span>
          </div>
        );
      })}

      {populatedTiers.length === 0 && working.length > 0 && (
        <div className="trace-ctx-tier trace-ctx-tier--flat">
          <span className="trace-ctx-tier-label">
            <span className="trace-ctx-tier-swatch trace-ctx-tier-swatch--flat" />
            working context
            <span className="trace-ctx-tier-count">({working.length})</span>
          </span>
          <PageList pages={working} />
          <span className="trace-ctx-tier-chars">—</span>
        </div>
      )}

      {scopeLinked.length > 0 && (
        <div className="trace-ctx-tier trace-ctx-tier--scope-linked">
          <span className="trace-ctx-tier-label">
            <span className="trace-ctx-tier-swatch trace-ctx-tier-swatch--scope-linked" />
            scope-linked
            <span className="trace-ctx-tier-count">({scopeLinked.length})</span>
          </span>
          <PageList pages={scopeLinked} />
          <span className="trace-ctx-tier-chars">—</span>
        </div>
      )}

      {preloaded.length > 0 && (
        <div className="trace-ctx-tier trace-ctx-tier--preloaded">
          <span className="trace-ctx-tier-label">
            <span className="trace-ctx-tier-swatch trace-ctx-tier-swatch--preloaded" />
            preloaded
            <span className="trace-ctx-tier-count">({preloaded.length})</span>
          </span>
          <PageList pages={preloaded} />
          <span className="trace-ctx-tier-chars">—</span>
        </div>
      )}

      {event.source_page_id && (
        <div className="trace-kv">
          <span className="trace-kv-key">source</span>
          <Link
            href={`/pages/${event.source_page_id}`}
            className="trace-kv-value trace-ctx-source-link"
          >
            {event.source_page_id.slice(0, 8)}
          </Link>
        </div>
      )}

      {event.context_text && (
        <details className="trace-ctx-prompt">
          <summary className="trace-ctx-prompt-summary">
            <span className="trace-ctx-prompt-caret" />
            context text
            <span className="trace-ctx-prompt-chars">
              {promptChars.toLocaleString()} ch
            </span>
          </summary>
          <pre className="trace-ctx-prompt-body">{event.context_text}</pre>
        </details>
      )}

      {event.budget != null && (
        <div className="trace-kv">
          <span className="trace-kv-key">budget</span>
          <span className="trace-kv-value">{event.budget}</span>
        </div>
      )}
    </div>
  );
}
