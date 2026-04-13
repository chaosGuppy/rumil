import type { Worldview, WorldviewNode } from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8099";

interface ApiNode {
  id: string;
  node_type: string;
  headline: string;
  content: string;
  credence: number | null;
  robustness: number | null;
  importance?: number;
  source_ids: string;
  created_at: string;
  created_by: string;
  children: ApiNode[];
}

function transformNode(api: ApiNode): WorldviewNode {
  let sourceIds: string[] = [];
  try {
    sourceIds = JSON.parse(api.source_ids);
  } catch {
    /* empty */
  }
  return {
    node_type: api.node_type as WorldviewNode["node_type"],
    headline: api.headline,
    content: api.content,
    credence: api.credence,
    robustness: api.robustness,
    importance: api.importance ?? 0,
    source_page_ids: sourceIds,
    created_by: api.created_by || "system",
    children: api.children.map(transformNode),
  };
}

export async function fetchWorldview(
  workspace: string = "default",
): Promise<Worldview> {
  const res = await fetch(`${API_BASE}/api/workspaces/${workspace}/tree`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  const root: ApiNode = await res.json();

  return {
    question_id: root.id,
    question_headline: root.headline,
    summary: root.content,
    nodes: root.children.map(transformNode),
    generated_at: root.created_at,
  };
}

export interface WorkspaceInfo {
  id: string;
  name: string;
  created_at: string;
  node_count: number;
  run_count: number;
  pending_suggestions: number;
}

export async function fetchWorkspaces(): Promise<WorkspaceInfo[]> {
  const res = await fetch(`${API_BASE}/api/workspaces`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function createWorkspace(
  name: string,
  question: string,
): Promise<{ id: string; name: string; root_node_id: string | null }> {
  const res = await fetch(
    `${API_BASE}/api/workspaces?name=${encodeURIComponent(name)}&question=${encodeURIComponent(question)}`,
    { method: "POST" },
  );
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export interface Suggestion {
  id: string;
  suggestion_type: string;
  target_node_id: string;
  target_headline: string | null;
  payload: string;
  status: string;
  created_at: string;
}

export async function fetchSuggestions(
  workspace: string = "default",
  status: string = "pending",
): Promise<Suggestion[]> {
  const res = await fetch(
    `${API_BASE}/api/workspaces/${workspace}/suggestions?status=${status}`,
  );
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function respondToSuggestion(
  id: string,
  action: "accept" | "reject",
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/suggestions/${id}/${action}`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
}

export interface SourceInfo {
  id: string;
  workspace_id: string;
  title: string;
  url: string;
  abstract: string;
  created_at: string;
}

export interface SourceFull extends SourceInfo {
  content: string;
  extra: string;
}

export async function fetchSources(
  workspace: string,
): Promise<SourceInfo[]> {
  const res = await fetch(
    `${API_BASE}/api/workspaces/${workspace}/sources`,
  );
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function fetchSourceByShortId(
  shortId: string,
): Promise<SourceFull | null> {
  const res = await fetch(
    `${API_BASE}/api/sources/short/${shortId}`,
  );
  if (!res.ok) return null;
  const data = await res.json();
  if (data.error) return null;
  return data;
}
