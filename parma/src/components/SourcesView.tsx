"use client";

import { useEffect, useMemo, useState, useCallback } from "react";
import { fetchSources, fetchPageByShortId } from "@/lib/api";
import type { Page } from "@/lib/types";

interface SourcesViewProps {
  projectId: string;
  onOpenDrawer: (source: Page) => void;
}

function hostname(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function normalizeUrl(url: string): string {
  try {
    const u = new URL(url);
    u.hash = "";
    u.search = "";
    const path = u.pathname.replace(/\/+$/, "");
    return `${u.hostname.replace(/^www\./, "")}${path}`.toLowerCase();
  } catch {
    return url.toLowerCase();
  }
}

const DUP_REF_RE = /duplicate[^.]*?\b([0-9a-f]{8})\b/i;

interface SourceGroup {
  canonical: Page;
  alts: Page[];
}

function buildGroups(sources: Page[]): SourceGroup[] {
  const byShortId = new Map<string, Page>();
  for (const s of sources) byShortId.set(s.id.slice(0, 8), s);

  const parentOf = new Map<string, string>();
  const find = (id: string): string => {
    let cur = id;
    const seen: string[] = [];
    while (parentOf.has(cur)) {
      seen.push(cur);
      cur = parentOf.get(cur)!;
    }
    for (const n of seen) parentOf.set(n, cur);
    return cur;
  };
  const union = (a: string, b: string) => {
    const ra = find(a);
    const rb = find(b);
    if (ra !== rb) parentOf.set(ra, rb);
  };

  const byNormUrl = new Map<string, string>();
  for (const s of sources) {
    const url = (s.extra?.url as string) ?? "";
    if (!url) continue;
    const key = normalizeUrl(url);
    const existing = byNormUrl.get(key);
    if (existing) union(s.id, existing);
    else byNormUrl.set(key, s.id);
  }

  for (const s of sources) {
    const abstract = (s.abstract ?? "") + " " + (s.content ?? "").slice(0, 400);
    const m = abstract.match(DUP_REF_RE);
    if (m) {
      const refShort = m[1].toLowerCase();
      const ref = byShortId.get(refShort);
      if (ref && ref.id !== s.id) union(s.id, ref.id);
    }
    if (s.superseded_by) {
      const target = sources.find((o) => o.id === s.superseded_by);
      if (target) union(s.id, target.id);
    }
  }

  const clusters = new Map<string, Page[]>();
  for (const s of sources) {
    const root = find(s.id);
    const list = clusters.get(root) ?? [];
    list.push(s);
    clusters.set(root, list);
  }

  const groups: SourceGroup[] = [];
  for (const members of clusters.values()) {
    members.sort((a, b) => {
      if (a.is_superseded !== b.is_superseded) return a.is_superseded ? 1 : -1;
      const ad = Date.parse(a.created_at);
      const bd = Date.parse(b.created_at);
      if (Number.isFinite(ad) && Number.isFinite(bd) && ad !== bd) {
        return bd - ad;
      }
      return 0;
    });
    const [canonical, ...alts] = members;
    groups.push({ canonical, alts });
  }
  return groups;
}

function sourceYear(s: Page): string | null {
  const extra = s.extra ?? {};
  const candidates = [
    (extra as Record<string, unknown>).publication_date,
    (extra as Record<string, unknown>).published_at,
    (extra as Record<string, unknown>).fetched_at,
  ];
  for (const c of candidates) {
    if (typeof c === "string" && c.length >= 4) {
      const yr = c.slice(0, 4);
      if (/^\d{4}$/.test(yr)) return yr;
    }
  }
  return null;
}

function sourceDate(s: Page): string | null {
  const extra = s.extra ?? {};
  const candidates = [
    (extra as Record<string, unknown>).publication_date,
    (extra as Record<string, unknown>).published_at,
    (extra as Record<string, unknown>).fetched_at,
  ];
  for (const c of candidates) {
    if (typeof c === "string") {
      const d = new Date(c);
      if (!Number.isNaN(d.getTime())) {
        return d.toLocaleDateString("en-US", {
          year: "numeric",
          month: "short",
          day: "numeric",
        });
      }
    }
  }
  return null;
}

function matchesFilter(s: Page, q: string): boolean {
  if (!q) return true;
  const needle = q.toLowerCase();
  const url = (s.extra?.url as string) ?? "";
  const fields = [
    s.headline,
    s.abstract,
    url,
    url ? hostname(url) : "",
    s.id.slice(0, 8),
  ];
  return fields.some((f) => f && f.toLowerCase().includes(needle));
}

function groupMatchesFilter(g: SourceGroup, q: string): boolean {
  if (!q) return true;
  if (matchesFilter(g.canonical, q)) return true;
  return g.alts.some((a) => matchesFilter(a, q));
}

export function SourcesView({ projectId, onOpenDrawer }: SourcesViewProps) {
  const [sources, setSources] = useState<Page[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("");
  const [expandedAlts, setExpandedAlts] = useState<Set<string>>(new Set());

  useEffect(() => {
    setLoading(true);
    fetchSources(projectId)
      .then(setSources)
      .catch(() => setSources([]))
      .finally(() => setLoading(false));
  }, [projectId]);

  const handleOpen = useCallback(
    async (source: Page) => {
      const full = await fetchPageByShortId(source.id.slice(0, 8));
      if (full) onOpenDrawer(full);
    },
    [onOpenDrawer],
  );

  const toggleAlts = useCallback((groupId: string) => {
    setExpandedAlts((prev) => {
      const next = new Set(prev);
      if (next.has(groupId)) next.delete(groupId);
      else next.add(groupId);
      return next;
    });
  }, []);

  const groups = useMemo(() => buildGroups(sources), [sources]);

  const filteredGroups = useMemo(() => {
    const q = filter.trim();
    return groups.filter((g) => groupMatchesFilter(g, q));
  }, [groups, filter]);

  const { domains, totalUnique, hiddenCount } = useMemo(() => {
    const byDomain = new Map<string, SourceGroup[]>();
    for (const g of filteredGroups) {
      const url = (g.canonical.extra?.url as string) ?? "";
      const domain = url ? hostname(url) : "unknown";
      const list = byDomain.get(domain) ?? [];
      list.push(g);
      byDomain.set(domain, list);
    }
    const sortedDomains = [...byDomain.entries()].sort(
      (a, b) => b[1].length - a[1].length,
    );
    return {
      domains: sortedDomains,
      totalUnique: filteredGroups.length,
      hiddenCount: groups.length - filteredGroups.length,
    };
  }, [filteredGroups, groups.length]);

  if (loading) {
    return (
      <div className="sources-layout">
        <div className="sources-loading">Loading sources...</div>
      </div>
    );
  }

  const totalCaptures = sources.length;
  const duplicatesCollapsed = totalCaptures - groups.length;

  return (
    <div className="sources-layout">
      <div className="sources-scroll">
        <div className="sources-content">
          <header className="sources-header">
            <h1 className="sources-title">Sources</h1>
            <p className="sources-subtitle">
              {totalUnique} unique source{totalUnique !== 1 ? "s" : ""}
              {duplicatesCollapsed > 0 && (
                <> · {duplicatesCollapsed} duplicate capture{duplicatesCollapsed !== 1 ? "s" : ""} collapsed</>
              )}
              {hiddenCount > 0 && (
                <> · {hiddenCount} hidden by filter</>
              )}
              . Click a card to read the full document.
            </p>
            <div className="sources-filter-row">
              <input
                type="search"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="Filter by title, URL, or domain..."
                className="sources-filter-input"
                aria-label="Filter sources"
              />
              {filter && (
                <button
                  type="button"
                  className="sources-filter-clear"
                  onClick={() => setFilter("")}
                  aria-label="Clear filter"
                >
                  clear
                </button>
              )}
            </div>
          </header>

          {domains.length === 0 ? (
            <div className="sources-empty">
              {sources.length === 0
                ? "No sources in this project yet."
                : "No sources match the filter."}
            </div>
          ) : (
            domains.map(([domain, domainGroups]) => (
              <section key={domain} className="sources-domain-group">
                <h2 className="sources-domain-label">
                  {domain.toUpperCase()}
                  <span className="sources-domain-count">
                    · {domainGroups.length}
                  </span>
                </h2>
                <div className="sources-list">
                  {domainGroups.map((g) => {
                    const s = g.canonical;
                    const url = (s.extra?.url as string) ?? "";
                    const date = sourceDate(s);
                    const year = sourceYear(s);
                    const altCount = g.alts.length;
                    const expanded = expandedAlts.has(s.id);
                    return (
                      <div key={s.id} className="sources-card-wrap">
                        <button
                          className="sources-card"
                          onClick={() => handleOpen(s)}
                        >
                          <div className="sources-card-top">
                            <span className="sources-card-id">
                              {s.id.slice(0, 8)}
                            </span>
                            {year && (
                              <span className="sources-card-year" title={date ?? year}>
                                {year}
                              </span>
                            )}
                            {url && (
                              <a
                                href={url}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="sources-card-ext"
                                onClick={(e) => e.stopPropagation()}
                              >
                                ↗
                              </a>
                            )}
                          </div>
                          <div className="sources-card-title">{s.headline}</div>
                          {s.abstract && (
                            <div className="sources-card-abstract">
                              {s.abstract.slice(0, 160)}
                              {s.abstract.length > 160 ? "..." : ""}
                            </div>
                          )}
                        </button>
                        {altCount > 0 && (
                          <div className="sources-card-alts">
                            <button
                              type="button"
                              className="sources-card-alts-toggle"
                              onClick={() => toggleAlts(s.id)}
                              aria-expanded={expanded}
                            >
                              <span className="sources-card-alts-caret">
                                {expanded ? "▾" : "▸"}
                              </span>
                              {altCount + 1} captures
                              <span className="sources-card-alts-hint">
                                {expanded ? "hide alts" : "show alts"}
                              </span>
                            </button>
                            {expanded && (
                              <ul className="sources-card-alts-list">
                                {g.alts.map((alt) => {
                                  const altUrl = (alt.extra?.url as string) ?? "";
                                  const altDate = sourceDate(alt);
                                  return (
                                    <li key={alt.id}>
                                      <button
                                        type="button"
                                        className="sources-card-alt"
                                        onClick={() => handleOpen(alt)}
                                      >
                                        <span className="sources-card-alt-id">
                                          {alt.id.slice(0, 8)}
                                        </span>
                                        <span className="sources-card-alt-title">
                                          {alt.headline}
                                        </span>
                                        {altDate && (
                                          <span className="sources-card-alt-date">
                                            {altDate}
                                          </span>
                                        )}
                                        {altUrl && (
                                          <a
                                            href={altUrl}
                                            target="_blank"
                                            rel="noopener noreferrer"
                                            className="sources-card-alt-ext"
                                            onClick={(e) => e.stopPropagation()}
                                          >
                                            ↗
                                          </a>
                                        )}
                                      </button>
                                    </li>
                                  );
                                })}
                              </ul>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </section>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
