"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import type { ProjectStatsOut, Project } from "@/api";

import { CLIENT_API_BASE as API_BASE } from "@/api-config";
import StagedBanner from "@/components/staged-banner";
import { WorkspaceIndicator } from "@/components/workspace-indicator";
import { StatsView } from "@/components/stats-view";
import { useDocumentTitle } from "@/lib/use-document-title";

function stagedQs(stagedRunId: string | null): string {
  return stagedRunId ? `?staged_run_id=${stagedRunId}` : "";
}

export default function ProjectStatsPage() {
  const params = useParams<{ projectId: string }>();
  const projectId = params.projectId;
  const searchParams = useSearchParams();
  const stagedRunId = searchParams.get("staged_run_id");
  const stagedQ = stagedQs(stagedRunId);

  const [projectName, setProjectName] = useState<string>();
  const [data, setData] = useState<ProjectStatsOut | null>(null);

  useDocumentTitle(projectName ? `${projectName} · stats` : null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${API_BASE}/api/projects/${projectId}`, { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : null))
      .then((d: Project | null) => {
        if (d) setProjectName(d.name);
      });
  }, [projectId]);

  useEffect(() => {
    setLoading(true);
    setError(null);
    fetch(`${API_BASE}/api/projects/${projectId}/stats${stagedQ}`, {
      cache: "no-store",
    })
      .then(async (res) => {
        if (!res.ok) throw new Error(`${res.status}`);
        return (await res.json()) as ProjectStatsOut;
      })
      .then((d) => {
        setData(d);
        setLoading(false);
      })
      .catch((e) => {
        setError(String(e));
        setLoading(false);
      });
  }, [projectId, stagedQ]);

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
          gap: 0.75rem;
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
        .stats-loading, .stats-error {
          padding: 4rem 1rem;
          text-align: center;
          color: var(--color-muted);
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.8rem;
          letter-spacing: 0.04em;
        }
        .stats-loading { animation: pulse 1.5s ease infinite; }
        @keyframes pulse {
          0%, 100% { opacity: 0.5; }
          50% { opacity: 1; }
        }
      `}</style>

      <WorkspaceIndicator projectId={projectId} projectName={projectName} />

      {stagedRunId && (
        <StagedBanner
          runId={stagedRunId}
          pageUrl={`/projects/${projectId}/stats`}
        />
      )}

      <div className="stats-header">
        <div>
          <h1>Statistics</h1>
          <div className="subtitle">project overview</div>
        </div>
        <div className="stats-nav">
          <Link href={`/projects/${projectId}${stagedQ}`}>Pages</Link>
        </div>
      </div>

      {loading ? (
        <div className="stats-loading">Computing statistics…</div>
      ) : error ? (
        <div className="stats-error">Failed to load stats: {error}</div>
      ) : data ? (
        <StatsView data={data} />
      ) : null}
    </main>
  );
}
