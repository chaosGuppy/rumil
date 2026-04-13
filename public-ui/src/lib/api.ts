import type { Worldview, WorldviewNode, NodeLink } from "./types";

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
  superseded_by?: string | null;
  children: ApiNode[];
}

interface ApiLink {
  id: string;
  source_id: string;
  target_id: string;
  link_type: string;
  strength: number | null;
  reasoning: string;
}

function transformNode(api: ApiNode): WorldviewNode {
  let sourceIds: string[] = [];
  try {
    sourceIds = JSON.parse(api.source_ids);
  } catch {
    /* empty */
  }
  return {
    id: api.id,
    node_type: api.node_type as WorldviewNode["node_type"],
    headline: api.headline,
    content: api.content,
    credence: api.credence,
    robustness: api.robustness,
    importance: api.importance ?? 0,
    source_page_ids: sourceIds,
    created_by: api.created_by || "system",
    superseded_by: api.superseded_by || null,
    children: api.children.map(transformNode),
  };
}

function attachLinks(node: WorldviewNode, linksBySource: Map<string, NodeLink[]>, linksByTarget: Map<string, NodeLink[]>): void {
  if (node.id) {
    node.links_out = linksBySource.get(node.id) ?? [];
    node.links_in = linksByTarget.get(node.id) ?? [];
  }
  for (const child of node.children) {
    attachLinks(child, linksBySource, linksByTarget);
  }
}

export async function fetchWorldview(
  workspace: string = "default",
): Promise<Worldview> {
  const res = await fetch(`${API_BASE}/api/workspaces/${workspace}/tree`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  const data = await res.json();
  const root: ApiNode = data;
  const apiLinks: ApiLink[] = data.links ?? [];

  const links: NodeLink[] = apiLinks.map((l) => ({
    id: l.id,
    source_id: l.source_id,
    target_id: l.target_id,
    link_type: l.link_type as NodeLink["link_type"],
    strength: l.strength,
    reasoning: l.reasoning,
  }));

  const linksBySource = new Map<string, NodeLink[]>();
  const linksByTarget = new Map<string, NodeLink[]>();
  for (const link of links) {
    const src = linksBySource.get(link.source_id) ?? [];
    src.push(link);
    linksBySource.set(link.source_id, src);
    const tgt = linksByTarget.get(link.target_id) ?? [];
    tgt.push(link);
    linksByTarget.set(link.target_id, tgt);
  }

  const nodes = root.children.map(transformNode);
  for (const node of nodes) {
    attachLinks(node, linksBySource, linksByTarget);
  }

  return {
    question_id: root.id,
    question_headline: root.headline,
    summary: root.content === root.headline ? "" : root.content,
    nodes,
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
): Promise<Record<string, unknown>> {
  const res = await fetch(`${API_BASE}/api/suggestions/${id}/${action}`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export interface ConceptInfo {
  id: string;
  headline: string;
  content: string;
}

export async function fetchConcepts(
  workspace: string,
): Promise<ConceptInfo[]> {
  const res = await fetch(
    `${API_BASE}/api/workspaces/${workspace}/concepts`,
  );
  if (!res.ok) return [];
  return res.json();
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
