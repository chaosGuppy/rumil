"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState, useMemo, useCallback, useRef } from "react";
import type { Page, PageType, PaginatedPagesOut, Project, RunListItemOut } from "@/api";

import { CLIENT_API_BASE as API_BASE } from "@/api-config";
import { useStagedRun } from "@/lib/staged-run-context";
import { withStagedRun } from "@/lib/staged-run-href";
import { WorkspaceIndicator } from "@/components/workspace-indicator";
import { useCurrentUser } from "@/lib/use-current-user";
import { useDocumentTitle } from "@/lib/use-document-title";

const PAGE_TYPES: PageType[] = [
  "question",
  "claim",
  "judgement",
  "view",
  "source",
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
  wiki: {
    accent: "var(--type-wiki)",
    bg: "var(--type-wiki-bg)",
    bgHover: "var(--type-wiki-bg-hover)",
    border: "var(--type-wiki-border)",
  },
  view: {
    accent: "var(--type-view)",
    bg: "var(--type-view-bg)",
    bgHover: "var(--type-view-bg-hover)",
    border: "var(--type-view-border)",
  },
  view_item: {
    accent: "var(--type-view-item)",
    bg: "var(--type-view-item-bg)",
    bgHover: "var(--type-view-item-bg-hover)",
    border: "var(--type-view-item-border)",
  },
};

function pageHref(page: Page, stagedRunId?: string | null): string {
  return withStagedRun(`/pages/${page.id}`, stagedRunId);
}

function epistemicLabel(page: Page) {
  const parts: string[] = [];
  if (page.credence != null) parts.push(`C${page.credence}`);
  if (page.robustness != null) parts.push(`R${page.robustness}`);
  if (parts.length === 0) return null;
  return <span className="ep-label">{parts.join("/")}</span>;
}

export default function PagesIndexPage() {
  const params = useParams<{ projectId: string }>();
  const projectId = params.projectId;

  const PAGE_SIZE = 50;

  const [projectName, setProjectName] = useState<string>();
  const [pages, setPages] = useState<Page[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [currentOffset, setCurrentOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [activeType, setActiveType] = useState<PageType | null>(null);
  const [runs, setRuns] = useState<RunListItemOut[]>([]);
  const [showSuperseded, setShowSuperseded] = useState(false);
  const { activeStagedRunId, setActiveStagedRunId } = useStagedRun();
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined);
  const currentUser = useCurrentUser();
  const isAdmin = currentUser?.is_admin ?? false;

  useDocumentTitle(projectName);

  const onSearchChange = useCallback((value: string) => {
    setSearch(value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      setDebouncedSearch(value);
      setCurrentOffset(0);
    }, 300);
  }, []);

  useEffect(() => {
    fetch(`${API_BASE}/api/projects/${projectId}`, { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : null))
      .then((data: Project | null) => {
        if (data) setProjectName(data.name);
      });
  }, [projectId]);

  useEffect(() => {
    setLoading(true);
    const params = new URLSearchParams();
    if (showSuperseded) params.set("active_only", "false");
    if (activeStagedRunId) params.set("staged_run_id", activeStagedRunId);
    if (debouncedSearch) params.set("search", debouncedSearch);
    if (activeType) params.set("page_type", activeType);
    params.set("offset", String(currentOffset));
    params.set("limit", String(PAGE_SIZE));
    const qs = params.toString();
    const url = `${API_BASE}/api/projects/${projectId}/pages?${qs}`;
    fetch(url, { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : { items: [], total_count: 0, offset: 0, limit: PAGE_SIZE }))
      .then((data: PaginatedPagesOut) => {
        setPages(data.items);
        setTotalCount(data.total_count);
        setLoading(false);
      });
  }, [projectId, showSuperseded, activeStagedRunId, debouncedSearch, activeType, currentOffset]);

  useEffect(() => {
    if (!isAdmin) {
      setRuns([]);
      return;
    }
    fetch(`${API_BASE}/api/projects/${projectId}/runs`, {
      cache: "no-store",
    })
      .then((res) => (res.ok ? res.json() : []))
      .then((data: RunListItemOut[]) => setRuns(data));
  }, [projectId, isAdmin]);

  const toggleType = (t: PageType) => {
    setActiveType((prev) => (prev === t ? null : t));
    setCurrentOffset(0);
  };

  const selectOnly = (t: PageType) => {
    setActiveType(t);
    setCurrentOffset(0);
  };

  const clearAll = () => {
    setActiveType(null);
    setCurrentOffset(0);
  };

  const totalPages = Math.ceil(totalCount / PAGE_SIZE);
  const currentPage = Math.floor(currentOffset / PAGE_SIZE) + 1;

  const goToPage = useCallback((page: number) => {
    setCurrentOffset((page - 1) * PAGE_SIZE);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

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
          display: flex;
          align-items: baseline;
          justify-content: space-between;
          gap: 1rem;
          flex-wrap: wrap;
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
        .pages-header-nav {
          display: flex;
          gap: 0.5rem;
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.7rem;
          letter-spacing: 0.06em;
          text-transform: uppercase;
        }
        .pages-header-nav a {
          color: var(--color-muted);
          text-decoration: none;
          border: 1px solid var(--color-border);
          padding: 0.35rem 0.7rem;
          transition: all 0.12s ease;
        }
        .pages-header-nav a:hover {
          color: var(--color-foreground);
          border-color: var(--color-accent);
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

        .ep-label {
          font-size: 0.7rem;
          font-family: var(--font-geist-mono), monospace;
          color: var(--color-muted);
        }

        .superseded-badge {
          font-size: 0.65rem;
          color: var(--color-dim);
          text-decoration: line-through;
          opacity: 0.6;
        }

        .superseded-toggle {
          color: var(--color-muted);
          border-color: var(--color-border);
          opacity: 0.5;
        }
        .superseded-toggle:hover {
          opacity: 0.75;
        }
        .superseded-toggle.active {
          background: var(--type-judgement-bg);
          color: var(--type-judgement);
          border-color: var(--type-judgement-border);
          opacity: 1;
        }

        .human-icon {
          width: 0.8rem;
          height: 0.8rem;
          color: rgba(230, 180, 80, 0.7);
          vertical-align: -1px;
          margin-right: 0.25rem;
        }

        .page-row.human-created {
          border-left: 2px solid rgba(230, 180, 80, 0.7);
          background: rgba(230, 180, 80, 0.07);
        }
        .page-row.human-created:hover {
          background: rgba(230, 180, 80, 0.12);
        }

        .page-row.superseded {
          opacity: 0.4;
        }
        .page-row.superseded:hover {
          opacity: 0.7;
        }

        .empty-state {
          padding: 3rem 1rem;
          text-align: center;
          color: var(--color-muted);
          font-size: 0.9rem;
        }

        .pagination {
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 1rem;
          margin-top: 1.25rem;
          padding: 0.75rem 0;
        }
        .pagination-btn {
          font-size: 0.75rem;
          font-family: var(--font-geist-mono), monospace;
          letter-spacing: 0.04em;
          text-transform: uppercase;
          padding: 0.35rem 0.75rem;
          border: 1px solid var(--color-border);
          border-radius: 1px;
          background: var(--color-surface);
          color: var(--color-foreground);
          cursor: pointer;
          transition: all 0.12s ease;
        }
        .pagination-btn:hover:not(:disabled) {
          border-color: var(--color-accent);
        }
        .pagination-btn:disabled {
          opacity: 0.3;
          cursor: default;
        }
        .pagination-info {
          font-size: 0.75rem;
          font-family: var(--font-geist-mono), monospace;
          color: var(--color-muted);
        }

        .runs-section {
          margin-bottom: 2rem;
        }
        .runs-section h2 {
          font-size: 0.75rem;
          font-weight: 600;
          letter-spacing: 0.06em;
          text-transform: uppercase;
          color: var(--color-muted);
          margin: 0 0 0.5rem 0;
          font-family: var(--font-geist-mono), monospace;
        }
        .runs-list {
          display: flex;
          flex-direction: column;
          gap: 1px;
          background: var(--color-border);
          border: 1px solid var(--color-border);
        }
        .run-row {
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 0.5rem 0.75rem;
          background: var(--color-background);
          text-decoration: none;
          color: inherit;
          transition: background 0.1s ease;
          gap: 1rem;
        }
        .run-row:hover {
          background: var(--color-surface);
        }
        .run-question {
          font-size: 0.82rem;
          flex: 1;
          min-width: 0;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }
        .run-meta {
          display: flex;
          align-items: center;
          gap: 0.75rem;
          flex-shrink: 0;
        }
        .run-id {
          font-size: 0.7rem;
          font-family: var(--font-geist-mono), monospace;
          color: var(--color-muted);
        }
        .run-date {
          font-size: 0.7rem;
          color: var(--color-muted);
          font-family: var(--font-geist-mono), monospace;
        }
        .run-ab-badge {
          font-size: 0.65rem;
          font-weight: 700;
          font-family: var(--font-geist-mono), monospace;
          letter-spacing: 0.06em;
          color: #d4943a;
          background: rgba(212, 148, 58, 0.12);
          padding: 0.15rem 0.4rem;
          border-radius: 2px;
          flex-shrink: 0;
        }

        .run-staged-badge {
          font-size: 0.65rem;
          font-weight: 700;
          font-family: var(--font-geist-mono), monospace;
          letter-spacing: 0.06em;
          color: #5a8a7a;
          background: rgba(90, 138, 122, 0.1);
          padding: 0.15rem 0.4rem;
          border-radius: 2px;
          flex-shrink: 0;
        }
        .run-staged-link {
          text-decoration: none;
          color: inherit;
        }
        .run-staged-link:hover {
          text-decoration: underline;
          text-underline-offset: 2px;
          text-decoration-color: var(--color-muted);
        }
        .run-staged-toggle {
          font-size: 0.6rem;
          font-weight: 700;
          font-family: var(--font-geist-mono), monospace;
          letter-spacing: 0.06em;
          padding: 0.2rem 0.45rem;
          border-radius: 2px;
          border: 1px solid var(--color-border);
          background: transparent;
          color: var(--color-muted);
          cursor: pointer;
          flex-shrink: 0;
          transition: all 0.12s ease;
        }
        .run-staged-toggle:hover {
          border-color: #5a8a7a;
          color: #5a8a7a;
        }
        .run-staged-toggle.active {
          background: #5a8a7a;
          border-color: #5a8a7a;
          color: #fff;
        }
        .run-staged-toggle.active:hover {
          background: #4a7a6a;
          border-color: #4a7a6a;
        }
        .run-staged-active {
          background: rgba(90, 138, 122, 0.04);
          border-left: 2px solid #5a8a7a;
        }

        .staged-banner {
          display: flex;
          align-items: center;
          gap: 0.5rem;
          padding: 0.45rem 0.75rem;
          margin-bottom: 1rem;
          background: rgba(90, 138, 122, 0.06);
          border: 1px solid rgba(90, 138, 122, 0.2);
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.75rem;
          color: #5a8a7a;
          animation: bannerSlideIn 0.2s ease both;
        }
        @keyframes bannerSlideIn {
          from { opacity: 0; transform: translateY(-4px); }
          to { opacity: 1; transform: translateY(0); }
        }
        .staged-banner-indicator {
          width: 6px;
          height: 6px;
          border-radius: 50%;
          background: #5a8a7a;
          flex-shrink: 0;
          animation: indicatorPulse 2s ease infinite;
        }
        @keyframes indicatorPulse {
          0%, 100% { opacity: 0.5; }
          50% { opacity: 1; }
        }
        .staged-banner-text {
          flex: 1;
          letter-spacing: 0.02em;
        }
        .staged-banner-clear {
          font-size: 0.7rem;
          font-family: var(--font-geist-mono), monospace;
          letter-spacing: 0.04em;
          text-transform: uppercase;
          color: #5a8a7a;
          background: none;
          border: none;
          cursor: pointer;
          padding: 0.2rem 0.4rem;
          opacity: 0.7;
          transition: opacity 0.12s ease;
        }
        .staged-banner-clear:hover {
          opacity: 1;
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

          --type-wiki: #4e7f56;
          --type-wiki-bg: #f3f6f3;
          --type-wiki-bg-hover: #e9f0ea;
          --type-wiki-border: #a5c0a9;

          --type-view: #7a5a8c;
          --type-view-bg: #f6f3f8;
          --type-view-bg-hover: #eee9f2;
          --type-view-border: #b8a5c5;

          --type-view-item: #8c6a9e;
          --type-view-item-bg: #f7f4f9;
          --type-view-item-bg-hover: #f0eaf4;
          --type-view-item-border: #c0aece;
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

            --type-wiki: #6fa877;
            --type-wiki-bg: #0b140d;
            --type-wiki-bg-hover: #111d14;
            --type-wiki-border: #1a2e1f;

            --type-view: #a07ab8;
            --type-view-bg: #140e18;
            --type-view-bg-hover: #1c1424;
            --type-view-border: #2e2038;

            --type-view-item: #b08ac8;
            --type-view-item-bg: #150f1a;
            --type-view-item-bg-hover: #1e1526;
            --type-view-item-border: #32243e;
          }
          .run-staged-badge {
            color: #6aaa9f;
            background: rgba(106, 170, 159, 0.12);
          }
          .run-staged-toggle:hover {
            border-color: #6aaa9f;
            color: #6aaa9f;
          }
          .run-staged-toggle.active {
            background: #6aaa9f;
            border-color: #6aaa9f;
            color: #0b1413;
          }
          .run-staged-toggle.active:hover {
            background: #5a9a8f;
            border-color: #5a9a8f;
          }
          .run-staged-active {
            background: rgba(106, 170, 159, 0.04);
            border-left-color: #6aaa9f;
          }
          .staged-banner {
            color: #6aaa9f;
            background: rgba(106, 170, 159, 0.05);
            border-color: rgba(106, 170, 159, 0.15);
          }
          .staged-banner-indicator {
            background: #6aaa9f;
          }
          .staged-banner-clear {
            color: #6aaa9f;
          }
        }
      `}</style>

      <WorkspaceIndicator projectId={projectId} projectName={projectName} />

      <div className="pages-header">
        <div>
          <h1>Pages</h1>
          <div className="subtitle">{totalCount} total</div>
        </div>
        {isAdmin && (
          <div className="pages-header-nav">
            <Link href={`/projects/${projectId}/stats`}>Stats</Link>
          </div>
        )}
      </div>

      {runs.length > 0 && (
        <div className="runs-section">
          <h2>Recent Runs</h2>
          <div className="runs-list">
            {runs.map((r, i) => {
              const isStaged = !!r.staged;
              const isActive = activeStagedRunId === r.run_id;
              const displayId = r.run_id?.slice(0, 8) ?? "\u2014";
              const label = r.name || r.question_summary || "(no question)";

              if (isStaged && r.run_id) {
                return (
                  <div
                    key={r.run_id}
                    className={`run-row run-row-staged${isActive ? " run-staged-active" : ""}`}
                  >
                    <span className="run-staged-badge">STAGED</span>
                    <Link href={`/traces/${r.run_id}`} className="run-question run-staged-link">
                      {label}
                    </Link>
                    <div className="run-meta">
                      <span className="run-date">
                        {new Date(r.created_at).toLocaleString()}
                      </span>
                      <span className="run-id">{displayId}</span>
                    </div>
                    <button
                      className={`run-staged-toggle${isActive ? " active" : ""}`}
                      onClick={() => setActiveStagedRunId(isActive ? null : r.run_id!)}
                      title={isActive ? "Hide staged outputs" : "Show staged outputs"}
                    >
                      {isActive ? "hide result" : "show result"}
                    </button>
                  </div>
                );
              }

              return (
                <Link
                  key={r.run_id || i}
                  href={`/traces/${r.run_id}`}
                  className="run-row"
                >
                  <span className="run-question">{label}</span>
                  <div className="run-meta">
                    <span className="run-date">
                      {new Date(r.created_at).toLocaleString()}
                    </span>
                    <span className="run-id">{displayId}</span>
                  </div>
                </Link>
              );
            })}
          </div>
        </div>
      )}

      {activeStagedRunId && (
        <div className="staged-banner">
          <span className="staged-banner-indicator" />
          <span className="staged-banner-text">
            Viewing staged run {activeStagedRunId.slice(0, 8)}
          </span>
          <button
            className="staged-banner-clear"
            onClick={() => setActiveStagedRunId(null)}
          >
            Clear
          </button>
        </div>
      )}

      <div className="controls">
        <div className="search-row">
          <span className="search-icon">/</span>
          <input
            type="text"
            placeholder="Search summaries and content..."
            value={search}
            onChange={(e) => onSearchChange(e.target.value)}
          />
        </div>

        <div className="filter-row">
          {PAGE_TYPES.map((t) => {
            const cfg = TYPE_CONFIG[t];
            const isActive = activeType === t;
            const noFilters = activeType === null;
            return (
              <button
                key={t}
                className={`filter-chip ${isActive ? "active" : noFilters ? "" : "inactive"}`}
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
              </button>
            );
          })}
          <div className="filter-divider" />
          <button
            className={`filter-chip superseded-toggle ${showSuperseded ? "active" : ""}`}
            onClick={() => {
              setShowSuperseded((prev) => !prev);
              setCurrentOffset(0);
            }}
            title="Show superseded pages"
          >
            {showSuperseded ? "hide" : "show"} superseded
          </button>
          {activeType !== null && (
            <>
              <div className="filter-divider" />
              <button className="filter-all" onClick={clearAll}>
                Clear
              </button>
            </>
          )}
        </div>
      </div>

      {loading ? (
        <div className="loading-state">Loading pages...</div>
      ) : pages.length === 0 ? (
        <div className="empty-state">
          {totalCount === 0 && !debouncedSearch && activeType === null
            ? "No pages in this project yet."
            : "No pages match the current filters."}
        </div>
      ) : (
        <>
          <div className="results-meta">
            {totalCount} page{totalCount !== 1 ? "s" : ""}
            {totalPages > 1 && ` \u00b7 page ${currentPage} of ${totalPages}`}
          </div>
          <div className="pages-grid">
            {pages.map((p, i) => {
              const cfg = TYPE_CONFIG[p.page_type] || TYPE_CONFIG.source;
              return (
                <Link
                  key={p.id}
                  href={pageHref(p, activeStagedRunId)}
                  className={`page-row${p.provenance_model === "human" ? " human-created" : ""}${p.is_superseded ? " superseded" : ""}`}
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
                    <div className="row-summary">{p.headline}</div>
                    <div className="row-content">{p.content}</div>
                  </div>
                  <div className="row-meta">
                    <span className="row-id">
                      {p.provenance_model === "human" && (
                        <svg className="human-icon" viewBox="0 0 16 16" fill="currentColor">
                          <circle cx="8" cy="4.5" r="2.5" />
                          <path d="M3 14c0-2.8 2.2-5 5-5s5 2.2 5 5" />
                        </svg>
                      )}
                      {p.id.slice(0, 8)}
                    </span>
                    {epistemicLabel(p)}
                    {p.is_superseded && (
                      <span className="superseded-badge">superseded</span>
                    )}
                  </div>
                </Link>
              );
            })}
          </div>
          {totalPages > 1 && (
            <div className="pagination">
              <button
                className="pagination-btn"
                disabled={currentPage <= 1}
                onClick={() => goToPage(currentPage - 1)}
              >
                Prev
              </button>
              <span className="pagination-info">
                {currentPage} / {totalPages}
              </span>
              <button
                className="pagination-btn"
                disabled={currentPage >= totalPages}
                onClick={() => goToPage(currentPage + 1)}
              >
                Next
              </button>
            </div>
          )}
        </>
      )}
    </main>
  );
}
