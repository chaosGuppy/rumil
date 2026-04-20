"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import type { PageDetailOut, QuestionStatsOut, Project } from "@/api";

import { CLIENT_API_BASE as API_BASE } from "@/api-config";
import { WorkspaceIndicator } from "@/components/workspace-indicator";
import { StatsView } from "@/components/stats-view";
import { SubgraphView } from "@/components/subgraph-view";
import { useDocumentTitle } from "@/lib/use-document-title";
import { truncateHeadline } from "@/lib/page-titles";

function stagedQs(stagedRunId: string | null): string {
  return stagedRunId ? `?staged_run_id=${stagedRunId}` : "";
}

type LoadState =
  | { kind: "loading" }
  | { kind: "not-found" }
  | { kind: "not-question"; pageType: string; headline: string }
  | { kind: "error"; message: string }
  | {
      kind: "ready";
      data: QuestionStatsOut;
      headline: string;
      projectId: string;
    };

export default function QuestionStatsPage() {
  const params = useParams<{ pageId: string }>();
  const pageId = params.pageId;
  const searchParams = useSearchParams();
  const stagedRunId = searchParams.get("staged_run_id");
  const stagedQ = stagedQs(stagedRunId);

  const [projectName, setProjectName] = useState<string>();
  const [projectId, setProjectId] = useState<string>();
  const [state, setState] = useState<LoadState>({ kind: "loading" });

  const titleHeadline = state.kind === "ready" ? state.headline : null;
  const titleLabel = titleHeadline
    ? `question "${truncateHeadline(titleHeadline, 45)}" · stats`
    : null;
  const wsSuffix = projectName ? ` — ${projectName}` : "";
  useDocumentTitle(titleLabel ? `${titleLabel}${wsSuffix}` : null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const detailRes = await fetch(
          `${API_BASE}/api/pages/${pageId}/detail${stagedQ}`,
          { cache: "no-store" },
        );
        if (detailRes.status === 404) {
          if (!cancelled) setState({ kind: "not-found" });
          return;
        }
        if (!detailRes.ok) throw new Error(`detail ${detailRes.status}`);
        const detail = (await detailRes.json()) as PageDetailOut;
        if (cancelled) return;
        setProjectId(detail.page.project_id);

        if (detail.page.page_type !== "question") {
          setState({
            kind: "not-question",
            pageType: detail.page.page_type,
            headline: detail.page.headline,
          });
          return;
        }

        const statsRes = await fetch(
          `${API_BASE}/api/pages/${pageId}/stats${stagedQ}`,
          { cache: "no-store" },
        );
        if (!statsRes.ok) throw new Error(`stats ${statsRes.status}`);
        const data = (await statsRes.json()) as QuestionStatsOut;
        if (cancelled) return;
        setState({
          kind: "ready",
          data,
          headline: detail.page.headline,
          projectId: detail.page.project_id,
        });
      } catch (err) {
        if (!cancelled) {
          setState({ kind: "error", message: String(err) });
        }
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [pageId, stagedQ]);

  useEffect(() => {
    if (!projectId) return;
    fetch(`${API_BASE}/api/projects/${projectId}`, { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : null))
      .then((d: Project | null) => {
        if (d) setProjectName(d.name);
      });
  }, [projectId]);

  const leadingPanel =
    state.kind === "ready" ? (
      <>
        <div className="question-context">
        <style>{`
          .question-context {
            border: 1px solid var(--type-question-border);
            background: var(--type-question-bg);
            padding: 1rem 1.25rem;
            border-left: 3px solid var(--type-question);
          }
          .question-context .label {
            font-size: 0.65rem;
            font-weight: 600;
            letter-spacing: 0.14em;
            text-transform: uppercase;
            color: var(--type-question);
            font-family: var(--font-geist-mono), monospace;
            margin-bottom: 0.35rem;
          }
          .question-context .headline {
            font-size: 1rem;
            font-weight: 500;
            line-height: 1.4;
            margin-bottom: 0.55rem;
          }
          .question-context .meta {
            font-family: var(--font-geist-mono), monospace;
            font-size: 0.7rem;
            color: var(--color-muted);
            letter-spacing: 0.03em;
          }
          .question-context .meta strong {
            color: var(--type-question);
            font-weight: 600;
          }
        `}</style>
        <div className="label">Question</div>
        <div className="headline">{state.headline}</div>
        <div className="meta">
          <strong>{state.data.subgraph_page_count}</strong>{" "}
          {state.data.subgraph_page_count === 1 ? "page" : "pages"} within 2 hops
        </div>
        </div>
        <SubgraphView data={state.data.subgraph} anchorId={pageId} />
      </>
    ) : null;

  return (
    <main className="stats-page">
      <style>{`
        .stats-page {
          max-width: 72rem;
          margin: 0 auto;
          padding: 2rem;
          font-family: var(--font-geist-sans), system-ui, sans-serif;
        }
        .stats-header {
          margin-top: 1.5rem;
          margin-bottom: 2rem;
          display: flex;
          align-items: baseline;
          justify-content: space-between;
          gap: 1rem;
          flex-wrap: wrap;
        }
        .stats-header h1 {
          font-size: 1.6rem;
          font-weight: 700;
          letter-spacing: -0.02em;
          margin: 0 0 0.25rem 0;
        }
        .stats-header .subtitle {
          font-size: 0.75rem;
          color: var(--color-muted);
          font-family: var(--font-geist-mono), monospace;
          letter-spacing: 0.06em;
          text-transform: uppercase;
        }
        .stats-nav {
          display: flex;
          gap: 0.5rem;
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.7rem;
          letter-spacing: 0.06em;
          text-transform: uppercase;
        }
        .stats-nav a {
          color: var(--color-muted);
          text-decoration: none;
          border: 1px solid var(--color-border);
          padding: 0.35rem 0.7rem;
          transition: all 0.12s ease;
        }
        .stats-nav a:hover {
          color: var(--color-foreground);
          border-color: var(--color-accent);
        }
        .stats-loading, .stats-error, .stats-empty {
          padding: 4rem 1rem;
          text-align: center;
          color: var(--color-muted);
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.8rem;
          letter-spacing: 0.04em;
          border: 1px solid var(--color-border);
          background: var(--color-background);
        }
        .stats-loading { animation: pulse 1.5s ease infinite; }
        @keyframes pulse {
          0%, 100% { opacity: 0.5; }
          50% { opacity: 1; }
        }
        .stats-empty .hint {
          margin-top: 0.75rem;
          font-size: 0.7rem;
          opacity: 0.7;
        }
      `}</style>

      {projectId ? (
        <WorkspaceIndicator projectId={projectId} projectName={projectName} />
      ) : null}

      <div className="stats-header">
        <div>
          <h1>Statistics</h1>
          <div className="subtitle">question neighborhood · 2 hops</div>
        </div>
        <div className="stats-nav">
          <Link href={`/pages/${pageId}${stagedQ}`}>Page</Link>
          {projectId && (
            <Link href={`/projects/${projectId}/stats${stagedQ}`}>
              Project stats
            </Link>
          )}
        </div>
      </div>

      {state.kind === "loading" && (
        <div className="stats-loading">Computing statistics…</div>
      )}
      {state.kind === "not-found" && (
        <div className="stats-empty">
          Page not found.
        </div>
      )}
      {state.kind === "not-question" && (
        <div className="stats-empty">
          Stats are only available for question pages.
          <div className="hint">
            This page is a{" "}
            <span style={{ color: `var(--type-${state.pageType})` }}>
              {state.pageType}
            </span>
            .
          </div>
        </div>
      )}
      {state.kind === "error" && (
        <div className="stats-error">Failed to load stats: {state.message}</div>
      )}
      {state.kind === "ready" && (
        <StatsView
          data={state.data}
          leadingPanel={leadingPanel}
          anchorId={pageId}
        />
      )}
    </main>
  );
}
