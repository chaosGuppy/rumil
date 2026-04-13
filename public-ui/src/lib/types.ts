export type WorldviewNodeType =
  | "claim"
  | "hypothesis"
  | "evidence"
  | "uncertainty"
  | "context"
  | "question"
  | "judgement"
  | "concept";

export type LinkType = "supports" | "opposes" | "depends_on" | "related";

export interface NodeLink {
  id: string;
  source_id: string;
  target_id: string;
  link_type: LinkType;
  strength: number | null;
  reasoning: string;
}

export interface WorldviewNode {
  id?: string;
  node_type: WorldviewNodeType;
  headline: string;
  content: string;
  credence: number | null;
  robustness: number | null;
  importance?: number;
  source_page_ids: string[];
  created_by?: string;
  superseded_by?: string | null;
  links_out?: NodeLink[];
  links_in?: NodeLink[];
  children: WorldviewNode[];
}

export interface Worldview {
  question_id: string;
  question_headline: string;
  summary: string;
  nodes: WorldviewNode[];
  generated_at: string;
}
