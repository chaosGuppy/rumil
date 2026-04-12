import type { Worldview, WorldviewNode } from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8099";

interface ApiNode {
  id: string;
  node_type: string;
  headline: string;
  content: string;
  credence: number | null;
  robustness: number | null;
  source_ids: string;
  created_at: string;
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
    source_page_ids: sourceIds,
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
