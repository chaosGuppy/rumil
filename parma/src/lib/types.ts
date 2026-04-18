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
  | "depends_on"
  | "view_item"
  | "view_of"
  | "meta_for";

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
  provenance_call_id?: string;
  provenance_model?: string;
  run_id?: string;
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
  // Optional fields present on the backend PageLink model; not every link
  // populates them. view_item links use importance/section/position;
  // child_question links use impact_on_parent_question (0-10).
  importance?: number | null;
  section?: string | null;
  position?: number | null;
  impact_on_parent_question?: number | null;
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

// Mirrors AdversarialVerdictSummaryOut in src/rumil/api/schemas.py.
// Returned by /api/pages/{page_id}/adversarial-verdicts and the batch
// /api/adversarial-verdicts?page_ids=... endpoint. `stronger_side` is the
// synthesizer's read on which scout's case was stronger; `claim_holds`
// may diverge (a claim can survive even if the how-false side was
// rhetorically stronger). `expired` is computed server-side from
// sunset_after_days + verdict_created_at.
export type AdversarialStrongerSide = "how_true" | "how_false" | "tie";

export interface AdversarialVerdictSummary {
  verdict_page_id: string;
  target_page_id: string;
  stronger_side: AdversarialStrongerSide;
  claim_holds: boolean;
  confidence: number;
  rationale: string;
  concurrences: string[];
  dissents: string[];
  sunset_after_days: number | null;
  verdict_created_at: string;
  expired: boolean;
  page_created_at: string;
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
