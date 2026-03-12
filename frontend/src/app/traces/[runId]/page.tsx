import Link from "next/link";
import type { RunTraceOut, RealtimeConfigOut } from "@/api/types.gen";
import { TraceViewer } from "./trace-viewer";
import "./trace.css";

const API_BASE = process.env.API_BASE_URL || "http://localhost:8000";

async function getRunTrace(runId: string): Promise<RunTraceOut | null> {
  const res = await fetch(`${API_BASE}/api/runs/${runId}/trace`, {
    cache: "no-store",
  });
  if (!res.ok) return null;
  return res.json();
}

async function getRealtimeConfig(): Promise<RealtimeConfigOut | null> {
  try {
    const res = await fetch(`${API_BASE}/api/realtime/config`, {
      cache: "no-store",
    });
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}

export default async function TracePage({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = await params;
  const [trace, realtimeConfig] = await Promise.all([
    getRunTrace(runId),
    getRealtimeConfig(),
  ]);

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

  return (
    <main className="trace-page">
      <header className="trace-header">
        {trace.question && (
          <Link
            href={`/questions/${trace.question.id}`}
            className="trace-back-link"
          >
            &larr; {trace.question.summary}
          </Link>
        )}
        <div className="trace-title-row">
          <h1 className="trace-title">Execution Trace</h1>
          <span className="trace-run-id">{runId.slice(0, 8)}</span>
        </div>
      </header>
      <TraceViewer
        initialTrace={trace}
        runId={runId}
        realtimeConfig={realtimeConfig}
      />
    </main>
  );
}
