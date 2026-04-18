// Types matching rumil's API response shapes.
// Page and PageLink mirror the Pydantic models in src/rumil/models.py.

export type PageType =
  | "source"
  | "claim"
  | "question"
  | "judgement"
  | "concept"
  | "wiki"
  | "summary";

export type LinkType =
  | "consideration"
  | "child_question"
  | "supersedes"
  | "related"
  | "answers"
  | "variant"
  | "summarizes"
  | "cites"
  | "depends_on";

export type ConsiderationDirection = "supports" | "opposes" | "neutral";

export type LinkRole = "direct" | "structural";

export interface Page {
  id: string;
  page_type: PageType;
  headline: string;
  content: string;
  abstract: string;
  credence: number | null;
  robustness: number | null;
  importance: number | null;
  superseded_by: string | null;
  is_superseded: boolean;
  provenance_call_type: string;
  extra: Record<string, unknown>;
  created_at: string;
}

export interface PageLink {
  id: string;
  from_page_id: string;
  to_page_id: string;
  link_type: LinkType;
  direction: ConsiderationDirection | null;
  strength: number;
  reasoning: string;
  role: LinkRole;
}

export interface ViewItem {
  page: Page;
  links: PageLink[];
  section: string;
}

export interface ViewSection {
  name: string;
  description: string;
  items: ViewItem[];
}

export interface ViewHealth {
  total_pages: number;
  missing_credence: number;
  missing_importance: number;
  child_questions_without_judgements: number;
  max_depth: number;
}

export interface QuestionView {
  question: Page;
  sections: ViewSection[];
  health: ViewHealth;
}

export interface Project {
  id: string;
  name: string;
  created_at: string;
  hidden: boolean;
}

// Mirrors ProjectSummaryOut in src/rumil/api/schemas.py. Produced by the
// list_projects_summary RPC in a single SQL call.
export interface ProjectSummary {
  id: string;
  name: string;
  created_at: string;
  hidden: boolean;
  question_count: number;
  claim_count: number;
  call_count: number;
  last_activity_at: string;
}
