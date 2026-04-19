// Single source of truth: types come from rumil's OpenAPI spec via
// codegen (see ../../openapi-ts.config.ts). Regenerate with
// `pnpm generate-api` in parma, or use the repo-level
// `scripts/generate-api-types.sh` to regenerate both frontend and parma
// together.
//
// We re-export the generated names here so existing parma call sites
// (`import type { Page } from "@/lib/types"`) keep working. New code can
// import directly from `@/api/types.gen` if preferred.

import type {
  AdversarialVerdictSummaryOut,
  ProjectSummaryOut,
  QuestionViewOut,
  SearchResultOut,
  ViewHealthOut,
  ViewItemOut,
  ViewSectionOut,
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
export type ViewItem = ViewItemOut;
export type ViewSection = ViewSectionOut;
export type ViewHealth = ViewHealthOut;
export type QuestionView = QuestionViewOut;

// The generated AdversarialVerdictSummaryOut.stronger_side is a plain
// `string` (the Python model doesn't narrow it). Keep the literal union
// here so UI code can switch on it exhaustively.
export type AdversarialStrongerSide = "how_true" | "how_false" | "tie";
