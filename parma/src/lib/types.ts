// Single source of truth: types come from rumil's OpenAPI spec via
// codegen (see ../../openapi-ts.config.ts). Regenerate with
// `pnpm generate-api` in parma, or use the repo-level
// `scripts/generate-api-types.sh` to regenerate both frontend and parma
// together.
//
// We re-export the generated names here so existing parma call sites
// (`import type { Page } from "@/lib/types"`) keep working. New code can
// import directly from `@/api/types.gen` if preferred.
//
// A few shapes are kept hand-written because their backing endpoints
// don't have a FastAPI `response_model` declared yet, so they don't
// appear in the generated output — QuestionView is the main one. When
// those endpoints get response models, delete the hand-written copy here
// and regenerate.

import type {
  AdversarialVerdictSummaryOut,
  ProjectSummaryOut,
  SearchResultOut,
} from "@/api/types.gen";

export type {
  ConsiderationDirection,
  LinkRole,
  LinkType,
  Page,
  PageLink,
  PageType,
  Project,
} from "@/api/types.gen";

// Server-side types the generator assigns an *Out suffix (FastAPI
// response-model convention). Aliased here for call-site ergonomics —
// and so the old import path keeps working.
export type AdversarialVerdictSummary = AdversarialVerdictSummaryOut;
export type ProjectSummary = ProjectSummaryOut;
export type SearchResult = SearchResultOut;

// The generated AdversarialVerdictSummaryOut.stronger_side is a plain
// `string` (the Python model doesn't narrow it). Keep the literal union
// here so UI code can switch on it exhaustively.
export type AdversarialStrongerSide = "how_true" | "how_false" | "tie";

// QuestionView shapes: the /api/questions/{id}/view endpoint has no
// FastAPI response_model, so nothing lands in the generated output.
// These mirror what the endpoint actually returns — if the backend shape
// drifts, TypeScript won't catch it here. Fix by adding a response_model
// to get_question_view in src/rumil/api/app.py.

import type { Page, PageLink } from "@/api/types.gen";

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
