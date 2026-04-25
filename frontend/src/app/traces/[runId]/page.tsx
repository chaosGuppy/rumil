import { Fragment } from "react";
import Link from "next/link";
import type { Metadata } from "next";
import type { RunTraceTreeOut, RealtimeConfigOut } from "@/api/types.gen";
import { TraceViewer } from "./trace-viewer";
import "./trace.css";

import { API_BASE, serverFetch } from "@/lib/api-base";
import { WorkspaceIndicator } from "@/components/workspace-indicator";
import { fetchProjectName } from "@/lib/fetch-project-name";
import { truncateHeadline } from "@/lib/page-titles";
import { withStagedRun } from "@/lib/staged-run-href";

async function getRunTraceTree(runId: string): Promise<RunTraceTreeOut | null> {
  const res = await serverFetch(`${API_BASE}/api/runs/${runId}/trace-tree`, {
    cache: "no-store",
  });
  if (!res.ok) return null;
  return res.json();
}

async function getRealtimeConfig(): Promise<RealtimeConfigOut | null> {
  try {
    const res = await serverFetch(`${API_BASE}/api/realtime/config`, {
      cache: "no-store",
    });
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ runId: string }>;
}): Promise<Metadata> {
  const { runId } = await params;
  const trace = await getRunTraceTree(runId);
  if (!trace) return { title: `trace ${runId.slice(0, 8)}` };
  const projectName = trace.question?.project_id
    ? await fetchProjectName(trace.question.project_id)
    : undefined;
  const headline = truncateHeadline(trace.question?.headline, 40);
  const mid = headline ? ` "${headline}"` : ` ${runId.slice(0, 8)}`;
  const suffix = projectName ? ` — ${projectName}` : "";
  return { title: `trace${mid}${suffix}` };
}

export default async function TracePage({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = await params;
  const [trace, realtimeConfig] = await Promise.all([
    getRunTraceTree(runId),
    getRealtimeConfig(),
  ]);

  const projectName = trace?.question?.project_id
    ? await fetchProjectName(trace.question.project_id)
    : undefined;

  if (!trace) {
    return (
      <main className="trace-page">
        <p className="trace-error">Run not found: {runId}</p>
        <Link href="/" className="trace-back-link">
          &larr; Back
        </Link>
      </main>
    );
  }

  const configEntries = Object.entries(trace.config ?? {}).sort(([a], [b]) =>
    a.localeCompare(b),
  );

  return (
    <main className="trace-page">
      {trace.question?.project_id && (
        <WorkspaceIndicator projectId={trace.question.project_id} projectName={projectName} />
      )}
      <header className="trace-header">
        {trace.question && (
          <Link
            href={withStagedRun(
              `/pages/${trace.question.id}`,
              trace.staged ? runId : null,
            )}
            className="trace-back-link"
          >
            &larr; {trace.question.headline}
          </Link>
        )}
        <div className="trace-title-row">
          <h1 className="trace-title">Execution Trace</h1>
          <span className="trace-run-id">{runId.slice(0, 8)}</span>
        </div>
      </header>
      {configEntries.length > 0 && (
        <div className="trace-config">
          <div className="trace-config-label">configuration</div>
          <div className="trace-config-table">
            {configEntries.map(([key, val]) => (
              <Fragment key={key}>
                <span className="trace-config-key">{key}</span>
                <span className="trace-config-val">{String(val)}</span>
              </Fragment>
            ))}
          </div>
        </div>
      )}
      <TraceViewer
        initialTrace={trace}
        runId={runId}
        realtimeConfig={realtimeConfig}
      />
    </main>
  );
}
