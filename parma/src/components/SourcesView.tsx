"use client";

import { useEffect, useState, useCallback } from "react";
import { fetchSources, fetchPageByShortId } from "@/lib/api";
import type { Page } from "@/lib/types";

interface SourcesViewProps {
  projectId: string;
  onOpenDrawer: (source: Page) => void;
}

function hostname(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

export function SourcesView({ projectId, onOpenDrawer }: SourcesViewProps) {
  const [sources, setSources] = useState<Page[]>([]);
  const [loading, setLoading] = useState(true);

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

  if (loading) {
    return (
      <div className="sources-layout">
        <div className="sources-loading">Loading sources...</div>
      </div>
    );
  }

  const byDomain = new Map<string, Page[]>();
  for (const s of sources) {
    const url = (s.extra?.url as string) ?? "";
    const domain = url ? hostname(url) : "unknown";
    const list = byDomain.get(domain) ?? [];
    list.push(s);
    byDomain.set(domain, list);
  }
  const domains = [...byDomain.entries()].sort(
    (a, b) => b[1].length - a[1].length,
  );

  return (
    <div className="sources-layout">
      <div className="sources-scroll">
        <div className="sources-content">
          <header className="sources-header">
            <h1 className="sources-title">Sources</h1>
            <p className="sources-subtitle">
              {sources.length} source{sources.length !== 1 ? "s" : ""} in this
              project. Click to read the full document.
            </p>
          </header>

          {domains.map(([domain, domainSources]) => (
            <section key={domain} className="sources-domain-group">
              <h2 className="sources-domain-label">{domain}</h2>
              <div className="sources-list">
                {domainSources.map((s) => {
                  const url = (s.extra?.url as string) ?? "";
                  return (
                    <button
                      key={s.id}
                      className="sources-card"
                      onClick={() => handleOpen(s)}
                    >
                      <div className="sources-card-top">
                        <span className="sources-card-id">
                          {s.id.slice(0, 8)}
                        </span>
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
                  );
                })}
              </div>
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}
