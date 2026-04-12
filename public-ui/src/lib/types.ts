export type WorldviewNodeType =
  | "claim"
  | "hypothesis"
  | "evidence"
  | "uncertainty"
  | "context"
  | "question";

export interface WorldviewNode {
  node_type: WorldviewNodeType;
  headline: string;
  content: string;
  credence: number | null;
  robustness: number | null;
  importance?: number;
  source_page_ids: string[];
  children: WorldviewNode[];
}

export interface Worldview {
  question_id: string;
  question_headline: string;
  summary: string;
  nodes: WorldviewNode[];
  generated_at: string;
}
