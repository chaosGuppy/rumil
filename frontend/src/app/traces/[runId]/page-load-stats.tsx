"use client";

import { useEffect, useMemo, useState } from "react";
import type { PageLoadStatsOut, PageLoadEventOut } from "@/api/types.gen";
import { API_BASE } from "@/lib/api-base";

const DETAIL_ORDER = ["content", "abstract", "headline"];

function DetailLabel({ detail }: { detail: string }) {
  const cls =
    detail === "content"
      ? "pls-detail-content"
      : detail === "abstract"
        ? "pls-detail-abstract"
        : "pls-detail-headline";
  return <span className={`pls-detail-label ${cls}`}>{detail}</span>;
}

function Cell({
  total,
  unique,
}: {
  total: number;
  unique: number;
}) {
  if (!total) return <td className="pls-cell pls-cell-empty">&mdash;</td>;
  return (
    <td className="pls-cell">
      <span className="pls-cell-total">{total}</span>
      <span className="pls-cell-unique">({unique}u)</span>
    </td>
  );
}

type GroupKey = string;

type GroupRow = {
  key: GroupKey;
  tagValues: Record<string, string>;
  byDetail: Record<string, { total: number; uniquePages: Set<string> }>;
  total: number;
  uniquePages: Set<string>;
};

function aggregate(
  events: PageLoadEventOut[],
  activeTags: string[],
  details: string[],
): GroupRow[] {
  const byKey = new Map<GroupKey, GroupRow>();
  for (const ev of events) {
    const tagValues: Record<string, string> = {};
    for (const t of activeTags) {
      tagValues[t] = ev.tags[t] ?? "";
    }
    const key = activeTags.map((t) => `${t}=${tagValues[t]}`).join("|") || "__all__";
    let row = byKey.get(key);
    if (!row) {
      row = {
        key,
        tagValues,
        byDetail: Object.fromEntries(
          details.map((d) => [d, { total: 0, uniquePages: new Set<string>() }]),
        ),
        total: 0,
        uniquePages: new Set<string>(),
      };
      byKey.set(key, row);
    }
    const bucket = row.byDetail[ev.detail];
    if (bucket) {
      bucket.total += 1;
      bucket.uniquePages.add(ev.page_id);
    }
    row.total += 1;
    row.uniquePages.add(ev.page_id);
  }
  return [...byKey.values()].sort((a, b) => b.total - a.total);
}

export function PageLoadStats({ runId }: { runId: string }) {
  const [stats, setStats] = useState<PageLoadStatsOut | null>(null);
  const [error, setError] = useState(false);
  const [activeTags, setActiveTags] = useState<string[]>(["call_type"]);

  useEffect(() => {
    fetch(`${API_BASE}/api/runs/${runId}/page-load-stats`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then(setStats)
      .catch(() => setError(true));
  }, [runId]);

  const tagKeys = useMemo(() => {
    if (!stats) return [];
    const keys = new Set<string>();
    for (const ev of stats.events) {
      for (const k of Object.keys(ev.tags)) keys.add(k);
    }
    return [...keys].sort();
  }, [stats]);

  const details = useMemo(() => {
    if (!stats) return DETAIL_ORDER;
    const present = new Set(stats.events.map((e) => e.detail));
    return DETAIL_ORDER.filter((d) => present.has(d));
  }, [stats]);

  const rows = useMemo(() => {
    if (!stats) return [];
    return aggregate(stats.events, activeTags, details);
  }, [stats, activeTags, details]);

  const totals = useMemo(() => {
    if (!stats) return { byDetail: {}, total: 0, uniquePages: new Set<string>() };
    const byDetail: Record<string, { total: number; uniquePages: Set<string> }> =
      Object.fromEntries(
        details.map((d) => [d, { total: 0, uniquePages: new Set<string>() }]),
      );
    const allUnique = new Set<string>();
    for (const ev of stats.events) {
      const bucket = byDetail[ev.detail];
      if (bucket) {
        bucket.total += 1;
        bucket.uniquePages.add(ev.page_id);
      }
      allUnique.add(ev.page_id);
    }
    return { byDetail, total: stats.events.length, uniquePages: allUnique };
  }, [stats, details]);

  if (error || !stats) return null;
  if (stats.total === 0) return null;

  const toggleTag = (tag: string) => {
    setActiveTags((prev) =>
      prev.includes(tag) ? prev.filter((t) => t !== tag) : [...prev, tag],
    );
  };

  return (
    <div className="pls-root">
      <div className="pls-header">
        <span className="pls-title">page loads</span>
        <span className="pls-total">
          {stats.total} total / {stats.total_unique} unique pages
        </span>
      </div>
      <div className="pls-controls">
        <span className="pls-controls-label">group by</span>
        <div className="pls-chips">
          {tagKeys.map((tag) => {
            const active = activeTags.includes(tag);
            return (
              <button
                key={tag}
                type="button"
                onClick={() => toggleTag(tag)}
                className={`pls-chip ${active ? "pls-chip-active" : ""}`}
              >
                {tag}
              </button>
            );
          })}
          {activeTags.length > 0 && (
            <button
              type="button"
              onClick={() => setActiveTags([])}
              className="pls-chip pls-chip-clear"
              title="Clear grouping"
            >
              clear
            </button>
          )}
        </div>
      </div>
      <div className="pls-table-wrap">
        <table className="pls-table">
          <thead>
            <tr>
              {activeTags.length === 0 ? (
                <th className="pls-th pls-th-call">all events</th>
              ) : (
                activeTags.map((t) => (
                  <th key={t} className="pls-th pls-th-call">
                    {t}
                  </th>
                ))
              )}
              {details.map((d) => (
                <th key={d} className="pls-th">
                  <DetailLabel detail={d} />
                </th>
              ))}
              <th className="pls-th">total</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.key} className="pls-row">
                {activeTags.length === 0 ? (
                  <td className="pls-call-type">&mdash;</td>
                ) : (
                  activeTags.map((t) => (
                    <td key={t} className="pls-call-type">
                      {row.tagValues[t] || (
                        <span className="pls-tag-empty">&empty;</span>
                      )}
                    </td>
                  ))
                )}
                {details.map((d) => (
                  <Cell
                    key={d}
                    total={row.byDetail[d]?.total ?? 0}
                    unique={row.byDetail[d]?.uniquePages.size ?? 0}
                  />
                ))}
                <Cell total={row.total} unique={row.uniquePages.size} />
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className="pls-row pls-row-total">
              <td
                className="pls-call-type pls-call-type-total"
                colSpan={Math.max(activeTags.length, 1)}
              >
                total
              </td>
              {details.map((d) => (
                <Cell
                  key={d}
                  total={totals.byDetail[d]?.total ?? 0}
                  unique={totals.byDetail[d]?.uniquePages.size ?? 0}
                />
              ))}
              <Cell total={totals.total} unique={totals.uniquePages.size} />
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  );
}
