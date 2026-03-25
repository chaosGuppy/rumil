import { queryOptions } from "@tanstack/react-query";
import type { RunTraceTreeOut, RealtimeConfigOut } from "@/api/types.gen";
import { CLIENT_API_BASE as API_BASE } from "@/api-config";

export const traceKeys = {
  all: ["traces"] as const,
  tree: (runId: string) => ["traces", runId, "tree"] as const,
  callEvents: (callId: string) => ["call-events", callId] as const,
};

export const realtimeKeys = {
  config: ["realtime", "config"] as const,
};

async function fetchRunTraceTree(runId: string): Promise<RunTraceTreeOut> {
  const res = await fetch(`${API_BASE}/api/runs/${runId}/trace-tree`);
  if (!res.ok) throw new Error(`Failed to fetch trace tree: ${res.status}`);
  return res.json();
}

async function fetchRealtimeConfig(): Promise<RealtimeConfigOut | null> {
  try {
    const res = await fetch(`${API_BASE}/api/realtime/config`);
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}

export function runTraceTreeOptions(
  runId: string,
  initialData?: RunTraceTreeOut,
) {
  return queryOptions({
    queryKey: traceKeys.tree(runId),
    queryFn: () => fetchRunTraceTree(runId),
    initialData,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data || data.calls.length === 0) return 3000;
      const allDone = data.calls.every(
        (n) => n.call.status === "complete" || n.call.status === "failed",
      );
      return allDone ? false : 3000;
    },
  });
}

export function realtimeConfigOptions(initialData?: RealtimeConfigOut | null) {
  return queryOptions({
    queryKey: realtimeKeys.config,
    queryFn: fetchRealtimeConfig,
    ...(initialData !== undefined && { initialData }),
    staleTime: Infinity,
  });
}
