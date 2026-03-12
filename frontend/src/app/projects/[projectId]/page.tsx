"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState, useMemo } from "react";
import type { Page, PageType } from "@/api";

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";

const PAGE_TYPES: PageType[] = [
  "question",
  "claim",
  "judgement",
  "source",
  "concept",
  "wiki",
];

const TYPE_CONFIG: Record<
  string,
  { accent: string; bg: string; bgHover: string; border: string }
> = {
  claim: {
    accent: "var(--type-claim)",
    bg: "var(--type-claim-bg)",
    bgHover: "var(--type-claim-bg-hover)",
    border: "var(--type-claim-border)",
  },
  question: {
    accent: "var(--type-question)",
    bg: "var(--type-question-bg)",
    bgHover: "var(--type-question-bg-hover)",
    border: "var(--type-question-border)",
  },
  judgement: {
    accent: "var(--type-judgement)",
    bg: "var(--type-judgement-bg)",
    bgHover: "var(--type-judgement-bg-hover)",
    border: "var(--type-judgement-border)",
  },
  source: {
    accent: "var(--type-source)",
    bg: "var(--type-source-bg)",
    bgHover: "var(--type-source-bg-hover)",
    border: "var(--type-source-border)",
  },
  concept: {
    accent: "var(--type-concept)",
    bg: "var(--type-concept-bg)",
    bgHover: "var(--type-concept-bg-hover)",
    border: "var(--type-concept-border)",
  },
  wiki: {
    accent: "var(--type-wiki)",
    bg: "var(--type-wiki-bg)",
    bgHover: "var(--type-wiki-bg-hover)",
    border: "var(--type-wiki-border)",
  },
};

function pageHref(page: Page): string {
  if (page.page_type === "question") return `/questions/${page.id}`;
  return `/pages/${page.id}`;
}

function epistemicBar(value: number) {
  const pct = (value / 5) * 100;
  return (
    <div className="ep-bar">
      <div className="ep-bar-fill" style={{ width: `${pct}%` }} />
    </div>
  );
}

export default function PagesIndexPage() {
  const params = useParams<{ projectId: string }>();
  const projectId = params.projectId;

  const [pages, setPages] = useState<Page[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [activeTypes, setActiveTypes] = useState<Set<PageType>>(
    new Set(PAGE_TYPES),
  );

  useEffect(() => {
    fetch(`${API_BASE}/api/projects/${projectId}/pages`, {
      cache: "no-store",
    })
      .then((res) => (res.ok ? res.json() : []))
      .then((data: Page[]) => {
        setPages(data);
        setLoading(false);
      });
  }, [projectId]);

  const toggleType = (t: PageType) => {
    setActiveTypes((prev) => {
      const next = new Set(prev);
      if (next.has(t)) {
        if (next.size > 1) next.delete(t);
      } else {
        next.add(t);
      }
      return next;
    });
  };

  const selectOnly = (t: PageType) => {
    setActiveTypes(new Set([t]));
  };

  const selectAll = () => {
    setActiveTypes(new Set(PAGE_TYPES));
  };

  const filtered = useMemo(() => {
    const q = search.toLowerCase();
    return pages.filter((p) => {
      if (!activeTypes.has(p.page_type)) return false;
      if (q && !p.summary.toLowerCase().includes(q) && !p.content.toLowerCase().includes(q))
        return false;
      return true;
    });
  }, [pages, search, activeTypes]);

  const typeCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const t of PAGE_TYPES) counts[t] = 0;
    for (const p of pages) counts[p.page_type] = (counts[p.page_type] || 0) + 1;
    return counts;
  }, [pages]);

  return (
    <main className="pages-index">
      <style>{`
        .pages-index {
          max-width: 72rem;
          margin: 0 auto;
          padding: 2rem;
          font-family: var(--font-geist-sans), system-ui, sans-serif;
        }

        .pages-index a.back-link {
          font-size: 0.8rem;
          color: var(--color-muted);
          text-decoration: none;
          letter-spacing: 0.03em;
          text-transform: uppercase;
        }
        .pages-index a.back-link:hover {
          color: var(--color-foreground);
        }

        .pages-header {
          margin-top: 1.5rem;
          margin-bottom: 2rem;
        }
        .pages-header h1 {
          font-size: 1.6rem;
          font-weight: 700;
          letter-spacing: -0.02em;
          margin: 0 0 0.25rem 0;
        }
        .pages-header .subtitle {
          font-size: 0.85rem;
          color: var(--color-muted);
          font-family: var(--font-geist-mono), monospace;
        }

        .controls {
          display: flex;
          flex-direction: column;
          gap: 0.75rem;
          margin-bottom: 1.5rem;
        }

        .search-row {
          position: relative;
        }
        .search-row input {
          width: 100%;
          padding: 0.6rem 0.75rem 0.6rem 2.25rem;
          font-size: 0.85rem;
          border: 1px solid var(--color-border);
          border-radius: 2px;
          background: var(--color-surface);
          color: var(--color-foreground);
          outline: none;
          transition: border-color 0.15s;
          font-family: inherit;
        }
        .search-row input:focus {
          border-color: var(--color-accent);
        }
        .search-row input::placeholder {
          color: var(--color-muted);
        }
        .search-icon {
          position: absolute;
          left: 0.7rem;
          top: 50%;
          transform: translateY(-50%);
          color: var(--color-muted);
          font-size: 0.9rem;
          pointer-events: none;
        }

        .filter-row {
          display: flex;
          align-items: center;
          gap: 0.375rem;
          flex-wrap: wrap;
        }
        .filter-chip {
          display: inline-flex;
          align-items: center;
          gap: 0.35rem;
          padding: 0.3rem 0.6rem;
          font-size: 0.75rem;
          font-weight: 500;
          border-radius: 1px;
          cursor: pointer;
          border: 1px solid transparent;
          transition: all 0.12s ease;
          user-select: none;
          font-family: var(--font-geist-mono), monospace;
          letter-spacing: 0.01em;
        }
        .filter-chip .count {
          opacity: 0.6;
          font-weight: 400;
        }
        .filter-chip.active {
          opacity: 1;
        }
        .filter-chip.inactive {
          opacity: 0.35;
          background: transparent !important;
          border-color: var(--color-border);
        }
        .filter-chip.inactive:hover {
          opacity: 0.6;
        }
        .filter-divider {
          width: 1px;
          height: 1.25rem;
          background: var(--color-border);
          margin: 0 0.25rem;
        }
        .filter-all {
          font-size: 0.7rem;
          color: var(--color-muted);
          cursor: pointer;
          padding: 0.3rem 0.4rem;
          border-radius: 1px;
          border: none;
          background: none;
          font-family: var(--font-geist-mono), monospace;
          letter-spacing: 0.02em;
          text-transform: uppercase;
        }
        .filter-all:hover {
          color: var(--color-foreground);
          background: var(--color-surface);
          border-radius: 1px;
        }

        .results-meta {
          font-size: 0.75rem;
          color: var(--color-muted);
          margin-bottom: 0.75rem;
          font-family: var(--font-geist-mono), monospace;
        }

        .pages-grid {
          display: grid;
          gap: 1px;
          background: var(--color-border);
          border: 1px solid var(--color-border);
          border-radius: 0;
          overflow: hidden;
        }

        .page-row {
          display: grid;
          grid-template-columns: 5.5rem 1fr auto;
          align-items: start;
          gap: 0.75rem;
          padding: 0.65rem 0.75rem;
          background: var(--color-background);
          text-decoration: none;
          color: inherit;
          transition: background 0.1s ease;
          animation: rowFadeIn 0.2s ease both;
        }
        .page-row:hover {
          background: var(--color-surface);
        }

        @keyframes rowFadeIn {
          from { opacity: 0; transform: translateY(2px); }
          to { opacity: 1; transform: translateY(0); }
        }

        .row-type {
          font-size: 0.7rem;
          font-weight: 600;
          font-family: var(--font-geist-mono), monospace;
          padding: 0.2rem 0.4rem;
          border-radius: 0;
          text-align: center;
          letter-spacing: 0.02em;
          white-space: nowrap;
          border-left: 2px solid transparent;
        }

        .row-body {
          min-width: 0;
        }
        .row-summary {
          font-size: 0.85rem;
          font-weight: 500;
          line-height: 1.35;
          margin-bottom: 0.15rem;
          overflow: hidden;
          text-overflow: ellipsis;
          display: -webkit-box;
          -webkit-line-clamp: 2;
          -webkit-box-orient: vertical;
        }
        .row-content {
          font-size: 0.75rem;
          color: var(--color-muted);
          line-height: 1.4;
          overflow: hidden;
          text-overflow: ellipsis;
          display: -webkit-box;
          -webkit-line-clamp: 1;
          -webkit-box-orient: vertical;
        }

        .row-meta {
          display: flex;
          flex-direction: column;
          align-items: flex-end;
          gap: 0.25rem;
          padding-top: 0.1rem;
          white-space: nowrap;
        }
        .row-id {
          font-size: 0.7rem;
          font-family: var(--font-geist-mono), monospace;
          color: var(--color-muted);
          opacity: 0.7;
        }

        .ep-bar {
          width: 2.5rem;
          height: 2px;
          background: var(--color-border);
          border-radius: 0;
          overflow: hidden;
        }
        .ep-bar-fill {
          height: 100%;
          background: var(--color-muted);
          border-radius: 0;
          transition: width 0.2s ease;
        }

        .superseded-badge {
          font-size: 0.65rem;
          color: var(--color-dim);
          text-decoration: line-through;
          opacity: 0.6;
        }

        .empty-state {
          padding: 3rem 1rem;
          text-align: center;
          color: var(--color-muted);
          font-size: 0.9rem;
        }

        .loading-state {
          padding: 4rem 1rem;
          text-align: center;
          color: var(--color-muted);
          font-size: 0.85rem;
          font-family: var(--font-geist-mono), monospace;
          letter-spacing: 0.05em;
          animation: pulse 1.5s ease infinite;
        }
        @keyframes pulse {
          0%, 100% { opacity: 0.5; }
          50% { opacity: 1; }
        }

        :root {
          --color-muted: #888;
          --color-dim: #aaa;
          --color-border: #ddd;
          --color-surface: #f6f6f6;
          --color-accent: #444;

          --type-claim: #4a6d9c;
          --type-claim-bg: #f3f5f8;
          --type-claim-bg-hover: #eaeff4;
          --type-claim-border: #b0bfcf;

          --type-question: #6b5b8a;
          --type-question-bg: #f4f3f7;
          --type-question-bg-hover: #edebf2;
          --type-question-border: #b5aec5;

          --type-judgement: #8c7040;
          --type-judgement-bg: #f7f5f0;
          --type-judgement-bg-hover: #f0ece2;
          --type-judgement-border: #c4b48a;

          --type-source: #6b7280;
          --type-source-bg: #f4f5f6;
          --type-source-bg-hover: #ededef;
          --type-source-border: #c0c3c8;

          --type-concept: #4a7f78;
          --type-concept-bg: #f2f6f5;
          --type-concept-bg-hover: #e8efed;
          --type-concept-border: #a3bdb8;

          --type-wiki: #4e7f56;
          --type-wiki-bg: #f3f6f3;
          --type-wiki-bg-hover: #e9f0ea;
          --type-wiki-border: #a5c0a9;
        }

        @media (prefers-color-scheme: dark) {
          :root {
            --color-muted: #6a6a6a;
            --color-dim: #4a4a4a;
            --color-border: #222;
            --color-surface: #111;
            --color-accent: #888;

            --type-claim: #7a9abb;
            --type-claim-bg: #0e1318;
            --type-claim-bg-hover: #131a22;
            --type-claim-border: #1e2a38;

            --type-question: #9388ad;
            --type-question-bg: #11101a;
            --type-question-bg-hover: #181524;
            --type-question-border: #242038;

            --type-judgement: #b8a46a;
            --type-judgement-bg: #141108;
            --type-judgement-bg-hover: #1c180f;
            --type-judgement-border: #2e2714;

            --type-source: #8a8f96;
            --type-source-bg: #101112;
            --type-source-bg-hover: #171819;
            --type-source-border: #242628;

            --type-concept: #6aaa9f;
            --type-concept-bg: #0b1413;
            --type-concept-bg-hover: #111d1b;
            --type-concept-border: #1a2e2a;

            --type-wiki: #6fa877;
            --type-wiki-bg: #0b140d;
            --type-wiki-bg-hover: #111d14;
            --type-wiki-border: #1a2e1f;
          }
        }
      `}</style>

      <Link href="/" className="back-link">
        &larr; Projects
      </Link>

      <div className="pages-header">
        <h1>Pages</h1>
        <div className="subtitle">{pages.length} total</div>
      </div>

      <div className="controls">
        <div className="search-row">
          <span className="search-icon">/</span>
          <input
            type="text"
            placeholder="Search summaries and content..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        <div className="filter-row">
          {PAGE_TYPES.map((t) => {
            const cfg = TYPE_CONFIG[t];
            const isActive = activeTypes.has(t);
            return (
              <button
                key={t}
                className={`filter-chip ${isActive ? "active" : "inactive"}`}
                style={
                  isActive
                    ? {
                        background: cfg.bg,
                        color: cfg.accent,
                        borderColor: cfg.border,
                      }
                    : {}
                }
                onClick={() => toggleType(t)}
                onDoubleClick={() => selectOnly(t)}
                title={`Click to toggle, double-click to isolate`}
              >
                {t}
                <span className="count">{typeCounts[t]}</span>
              </button>
            );
          })}
          <div className="filter-divider" />
          <button className="filter-all" onClick={selectAll}>
            All
          </button>
        </div>
      </div>

      {loading ? (
        <div className="loading-state">Loading pages...</div>
      ) : filtered.length === 0 ? (
        <div className="empty-state">
          {pages.length === 0
            ? "No pages in this project yet."
            : "No pages match the current filters."}
        </div>
      ) : (
        <>
          <div className="results-meta">
            {filtered.length === pages.length
              ? `${filtered.length} pages`
              : `${filtered.length} of ${pages.length} pages`}
          </div>
          <div className="pages-grid">
            {filtered.map((p, i) => {
              const cfg = TYPE_CONFIG[p.page_type] || TYPE_CONFIG.source;
              return (
                <Link
                  key={p.id}
                  href={pageHref(p)}
                  className="page-row"
                  style={{ animationDelay: `${Math.min(i * 15, 300)}ms` }}
                >
                  <span
                    className="row-type"
                    style={{
                      background: cfg.bg,
                      color: cfg.accent,
                      borderLeftColor: cfg.accent,
                    }}
                  >
                    {p.page_type}
                  </span>
                  <div className="row-body">
                    <div className="row-summary">{p.summary}</div>
                    <div className="row-content">{p.content}</div>
                  </div>
                  <div className="row-meta">
                    <span className="row-id">{p.id.slice(0, 8)}</span>
                    {epistemicBar(p.epistemic_status)}
                    {p.is_superseded && (
                      <span className="superseded-badge">superseded</span>
                    )}
                  </div>
                </Link>
              );
            })}
          </div>
        </>
      )}
    </main>
  );
}
