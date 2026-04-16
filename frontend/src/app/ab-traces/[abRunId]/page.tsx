import Link from "next/link";
import type { Metadata } from "next";
import type { AbRunTraceOut } from "@/api/types.gen";
import { ABTraceViewer } from "./ab-trace-viewer";
import "../../traces/[runId]/trace.css";

import { API_BASE, serverFetch } from "@/lib/api-base";
import { WorkspaceIndicator } from "@/components/workspace-indicator";
import { fetchProjectName } from "@/lib/fetch-project-name";
import { truncateHeadline } from "@/lib/page-titles";

async function getABRunTrace(abRunId: string): Promise<AbRunTraceOut | null> {
  const res = await serverFetch(`${API_BASE}/api/ab-runs/${abRunId}/trace`, {
    cache: "no-store",
  });
  if (!res.ok) return null;
  return res.json();
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ abRunId: string }>;
}): Promise<Metadata> {
  const { abRunId } = await params;
  const trace = await getABRunTrace(abRunId);
  if (!trace) return { title: `AB ${abRunId.slice(0, 8)}` };
  const projectName = trace.question?.project_id
    ? await fetchProjectName(trace.question.project_id)
    : undefined;
  const headline = truncateHeadline(trace.question?.headline, 40);
  const mid = headline ? ` "${headline}"` : ` ${abRunId.slice(0, 8)}`;
  const suffix = projectName ? ` — ${projectName}` : "";
  return { title: `AB${mid}${suffix}` };
}

export default async function ABTracePage({
  params,
}: {
  params: Promise<{ abRunId: string }>;
}) {
  const { abRunId } = await params;
  const trace = await getABRunTrace(abRunId);

  const projectName = trace?.question?.project_id
    ? await fetchProjectName(trace.question.project_id)
    : undefined;

  if (!trace) {
    return (
      <main className="trace-page">
        <p className="trace-error">AB run not found: {abRunId}</p>
        <Link href="/" className="trace-back-link">
          &larr; Back
        </Link>
      </main>
    );
  }

  return (
    <main className="ab-trace-page">
      {trace.question?.project_id && (
        <WorkspaceIndicator projectId={trace.question.project_id} projectName={projectName} />
      )}
      <header className="ab-trace-header">
        {trace.question && (
          <Link
            href={`/pages/${trace.question.id}`}
            className="trace-back-link"
          >
            &larr; {trace.question.headline}
          </Link>
        )}
        <div className="ab-trace-title-row">
          <h1 className="trace-title">AB Comparison</h1>
          <span className="ab-trace-badge">AB</span>
          <span className="trace-run-id">{abRunId.slice(0, 8)}</span>
        </div>
        {trace.name && (
          <div className="ab-trace-name">{trace.name}</div>
        )}
      </header>
      <ABTraceViewer trace={trace} />
    </main>
  );
}
