import { queryOptions } from "@tanstack/react-query";
import type { RunTraceOut, RealtimeConfigOut } from "@/api/types.gen";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export const traceKeys = {
  all: ["traces"] as const,
  detail: (runId: string) => ["traces", runId] as const,
};

export const realtimeKeys = {
  config: ["realtime", "config"] as const,
};

async function fetchRunTrace(runId: string): Promise<RunTraceOut> {
  const res = await fetch(`${API_BASE}/api/runs/${runId}/trace`);
  if (!res.ok) throw new Error(`Failed to fetch trace: ${res.status}`);
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

export function runTraceOptions(runId: string, initialData?: RunTraceOut) {
  return queryOptions({
    queryKey: traceKeys.detail(runId),
    queryFn: () => fetchRunTrace(runId),
    initialData,
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
