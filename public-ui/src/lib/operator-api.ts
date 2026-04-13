import type { RunSummary, RunDetail } from "./operator-types";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8099";

export async function fetchRuns(params: {
  workspace?: string;
  run_type?: "chat" | "orchestrate";
  status?: "running" | "completed" | "error";
  limit?: number;
  offset?: number;
} = {}): Promise<{ runs: RunSummary[]; total: number }> {
  const qs = new URLSearchParams();
  if (params.workspace) qs.set("workspace", params.workspace);
  if (params.run_type) qs.set("run_type", params.run_type);
  if (params.status) qs.set("status", params.status);
  qs.set("limit", String(params.limit ?? 50));
  qs.set("offset", String(params.offset ?? 0));
  const res = await fetch(`${API_BASE}/api/operator/runs?${qs}`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function fetchRunDetail(runId: string): Promise<RunDetail> {
  const res = await fetch(`${API_BASE}/api/operator/runs/${runId}`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}
