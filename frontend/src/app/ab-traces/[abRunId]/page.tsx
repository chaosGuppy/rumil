import Link from "next/link";
import type { AbRunTraceOut } from "@/api/types.gen";
import { ABTraceViewer } from "./ab-trace-viewer";
import "../../traces/[runId]/trace.css";

import { API_BASE } from "@/lib/api-base";

async function getABRunTrace(abRunId: string): Promise<AbRunTraceOut | null> {
  const res = await fetch(`${API_BASE}/api/ab-runs/${abRunId}/trace`, {
    cache: "no-store",
  });
  if (!res.ok) return null;
  return res.json();
}

export default async function ABTracePage({
  params,
}: {
  params: Promise<{ abRunId: string }>;
}) {
  const { abRunId } = await params;
  const trace = await getABRunTrace(abRunId);

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
