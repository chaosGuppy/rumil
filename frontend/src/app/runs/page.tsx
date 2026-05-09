"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import type { PaginatedRunsOut, RunListItemOut } from "@/api";

import { CLIENT_API_BASE as API_BASE } from "@/api-config";
import { clientFetch } from "@/lib/client-fetch";
import { useCurrentUser } from "@/lib/use-current-user";
import { useDocumentTitle } from "@/lib/use-document-title";

const PAGE_SIZE = 20;

export default function RecentRunsPage() {
  const currentUser = useCurrentUser();
  const isAdmin = currentUser?.is_admin ?? false;

  const [runs, setRuns] = useState<RunListItemOut[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [authChecked, setAuthChecked] = useState(false);

  useDocumentTitle("Recent runs");

  useEffect(() => {
    if (currentUser !== null) setAuthChecked(true);
  }, [currentUser]);

  useEffect(() => {
    if (!isAdmin) {
      setLoading(false);
      return;
    }
    setLoading(true);
    const params = new URLSearchParams({
      offset: String(offset),
      limit: String(PAGE_SIZE),
    });
    clientFetch(`${API_BASE}/api/admin/runs?${params}`, { cache: "no-store" })
      .then((res) =>
        res.ok ? res.json() : { items: [], total_count: 0, offset, limit: PAGE_SIZE },
      )
      .then((data: PaginatedRunsOut) => {
        setRuns(data.items as RunListItemOut[]);
        setTotalCount(data.total_count);
        setLoading(false);
      });
  }, [isAdmin, offset]);

  const totalPages = Math.max(1, Math.ceil(totalCount / PAGE_SIZE));
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  const goToPage = useCallback((page: number) => {
    setOffset((page - 1) * PAGE_SIZE);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  return (
    <main className="runs-index">
      <style>{`
        .runs-index {
          max-width: 72rem;
          margin: 0 auto;
          padding: 2rem;
          font-family: var(--font-geist-sans), system-ui, sans-serif;
        }
        .runs-index a.back-link {
          font-size: 0.8rem;
          color: var(--color-muted);
          text-decoration: none;
          letter-spacing: 0.03em;
          text-transform: uppercase;
        }
        .runs-index a.back-link:hover {
          color: var(--color-foreground);
        }
        .runs-header {
          margin-top: 1.5rem;
          margin-bottom: 1.5rem;
        }
        .runs-header h1 {
          font-size: 1.6rem;
          font-weight: 700;
          letter-spacing: -0.02em;
          margin: 0 0 0.25rem 0;
        }
        .runs-header .subtitle {
          font-size: 0.85rem;
          color: var(--color-muted);
          font-family: var(--font-geist-mono), monospace;
        }
        .runs-list {
          display: grid;
          gap: 1px;
          background: var(--color-border);
          border: 1px solid var(--color-border);
        }
        .run-row {
          display: grid;
          grid-template-columns: 9rem 1fr auto;
          align-items: start;
          gap: 0.75rem;
          padding: 0.65rem 0.75rem;
          background: var(--color-background);
          text-decoration: none;
          color: inherit;
          transition: background 0.1s ease;
          animation: rowFadeIn 0.2s ease both;
        }
        .run-row:hover {
          background: var(--color-surface);
        }
        @keyframes rowFadeIn {
          from { opacity: 0; transform: translateY(2px); }
          to { opacity: 1; transform: translateY(0); }
        }
        .run-project {
          font-size: 0.7rem;
          font-family: var(--font-geist-mono), monospace;
          color: var(--color-muted);
          text-decoration: none;
          padding-top: 0.15rem;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }
        .run-project:hover {
          color: var(--color-foreground);
          text-decoration: underline;
          text-underline-offset: 2px;
        }
        .run-project.unset {
          opacity: 0.5;
        }
        .run-body {
          min-width: 0;
        }
        .run-summary {
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
        .run-name {
          font-size: 0.72rem;
          color: var(--color-muted);
          font-family: var(--font-geist-mono), monospace;
        }
        .run-meta {
          display: flex;
          flex-direction: column;
          align-items: flex-end;
          gap: 0.25rem;
          padding-top: 0.1rem;
          white-space: nowrap;
        }
        .run-date {
          font-size: 0.7rem;
          color: var(--color-muted);
          font-family: var(--font-geist-mono), monospace;
        }
        .run-id {
          font-size: 0.7rem;
          font-family: var(--font-geist-mono), monospace;
          color: var(--color-muted);
          opacity: 0.7;
        }
        .run-staged-badge {
          font-size: 0.6rem;
          font-weight: 700;
          font-family: var(--font-geist-mono), monospace;
          letter-spacing: 0.06em;
          color: #5a8a7a;
          background: rgba(90, 138, 122, 0.1);
          padding: 0.1rem 0.35rem;
          border-radius: 2px;
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
          --color-border: #ddd;
          --color-surface: #f6f6f6;
          --color-accent: #444;
        }
        @media (prefers-color-scheme: dark) {
          :root {
            --color-muted: #6a6a6a;
            --color-border: #222;
            --color-surface: #111;
            --color-accent: #888;
          }
          .run-staged-badge {
            color: #6aaa9f;
            background: rgba(106, 170, 159, 0.12);
          }
        }
      `}</style>

      <Link href="/" className="back-link">
        ← Home
      </Link>

      <div className="runs-header">
        <h1>Recent runs</h1>
        <div className="subtitle">
          {totalCount} total · all projects
        </div>
      </div>

      {!authChecked ? (
        <div className="loading-state">Checking access...</div>
      ) : !isAdmin ? (
        <div className="empty-state">Admin access required.</div>
      ) : loading ? (
        <div className="loading-state">Loading runs...</div>
      ) : runs.length === 0 ? (
        <div className="empty-state">No runs yet.</div>
      ) : (
        <>
          <div className="runs-list">
            {runs.map((r, i) => {
              const displayId = r.run_id?.slice(0, 8) ?? "—";
              const label =
                r.question_summary || r.name || "(no question)";
              const projectLabel = r.project_name ?? r.project_id?.slice(0, 8) ?? "—";
              return (
                <Link
                  key={r.run_id ?? i}
                  href={r.run_id ? `/traces/${r.run_id}` : "#"}
                  className="run-row"
                  style={{ animationDelay: `${Math.min(i * 15, 200)}ms` }}
                >
                  {r.project_id ? (
                    <span
                      className="run-project"
                      onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        window.location.href = `/projects/${r.project_id}`;
                      }}
                      role="link"
                    >
                      {projectLabel}
                    </span>
                  ) : (
                    <span className="run-project unset">{projectLabel}</span>
                  )}
                  <div className="run-body">
                    <div className="run-summary">{label}</div>
                    {r.name && r.name !== label && (
                      <div className="run-name">{r.name}</div>
                    )}
                  </div>
                  <div className="run-meta">
                    <span className="run-date">
                      {new Date(r.created_at).toLocaleString()}
                    </span>
                    <span className="run-id">{displayId}</span>
                    {r.staged && <span className="run-staged-badge">STAGED</span>}
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
