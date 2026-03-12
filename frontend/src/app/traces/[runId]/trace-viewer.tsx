"use client";

import type { RunTraceOut } from "@/api/types.gen";
import { useRunTrace } from "@/lib/use-run-trace";
import { CallNode } from "./call-node";

export function TraceViewer({
  initialTrace,
  runId,
  realtimeConfig,
}: {
  initialTrace: RunTraceOut;
  runId: string;
  realtimeConfig: { url: string; anon_key: string } | null;
}) {
  const trace = useRunTrace(runId, initialTrace, realtimeConfig);

  return (
    <div className="trace-root">
      {trace.cost_usd != null && (
        <div className="trace-run-cost">
          Total cost: ${trace.cost_usd.toFixed(4)}
        </div>
      )}
      {trace.root_calls.map((ct) => (
        <CallNode key={ct.call.id} trace={ct} depth={0} />
      ))}
      {trace.root_calls.length === 0 && (
        <p className="trace-empty">
          No calls recorded for this run yet.
        </p>
      )}
    </div>
  );
}
