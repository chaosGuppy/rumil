import Link from "next/link";
import { TraceViewer } from "./trace-viewer";

const API_BASE = process.env.API_BASE_URL || "http://localhost:8000";

interface TraceEvent {
  event: string;
  ts: string;
  call_id: string;
  data: Record<string, unknown>;
}

interface CallOut {
  id: string;
  call_type: string;
  status: string;
  parent_call_id: string | null;
  scope_page_id: string | null;
  budget_allocated: number | null;
  budget_used: number;
  result_summary: string;
  review_json: Record<string, unknown>;
  created_at: string;
  completed_at: string | null;
}

interface CallTrace {
  call: CallOut;
  events: TraceEvent[];
  children: CallTrace[];
}

interface RunTrace {
  run_id: string;
  question: {
    id: string;
    summary: string;
    content: string;
    project_id: string;
  } | null;
  root_calls: CallTrace[];
}

async function getRunTrace(runId: string): Promise<RunTrace | null> {
  const res = await fetch(`${API_BASE}/api/runs/${runId}/trace`, {
    cache: "no-store",
  });
  if (!res.ok) return null;
  return res.json();
}

async function getRealtimeConfig(): Promise<{
  url: string;
  anon_key: string;
} | null> {
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
      <main className="max-w-6xl mx-auto p-8">
        <p className="text-red-500">Run not found: {runId}</p>
        <Link href="/" className="text-blue-600 hover:underline text-sm">
          &larr; Back
        </Link>
      </main>
    );
  }

  return (
    <main className="max-w-6xl mx-auto p-8">
      {trace.question && (
        <Link
          href={`/questions/${trace.question.id}`}
          className="text-blue-600 hover:underline text-sm"
        >
          &larr; {trace.question.summary}
        </Link>
      )}
      <h1 className="text-2xl font-semibold mt-2 mb-1">Execution Trace</h1>
      <p className="text-sm text-gray-500 mb-6 font-mono">
        run {runId.slice(0, 8)}
      </p>
      <TraceViewer
        initialTrace={trace}
        runId={runId}
        realtimeConfig={realtimeConfig}
      />
    </main>
  );
}
