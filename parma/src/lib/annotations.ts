// Annotation API client — wraps the /api/annotations endpoints shipped by
// the substrate (see marketplace-thread/28-annotation-primitives.md).
//
// The backend model is intentionally permissive: annotation_type is a string
// (span / counterfactual_tool_use / flag / endorsement), category is free
// text, and payload is an opaque dict. We lean on that here — no enum
// gymnastics, one create() and two list() functions.

import { API_BASE } from "./api";

// Mirror of rumil.models.AnnotationEvent. We don't auto-generate parma's
// types today, so this is hand-maintained. Keep the field set in sync with
// src/rumil/models.py::AnnotationEvent.
export interface AnnotationEvent {
  id: string;
  annotation_type: string;
  author_type: string;
  author_id: string;
  target_page_id: string | null;
  target_call_id: string | null;
  target_event_seq: number | null;
  span_start: number | null;
  span_end: number | null;
  category: string | null;
  note: string;
  payload: Record<string, unknown>;
  extra: Record<string, unknown>;
  run_id: string | null;
  project_id: string | null;
  staged: boolean;
  created_at: string;
}

export interface AnnotationCreateRequest {
  annotation_type: "span" | "counterfactual_tool_use" | "flag" | "endorsement";
  target_page_id?: string | null;
  target_call_id?: string | null;
  target_event_seq?: number | null;
  span_start?: number | null;
  span_end?: number | null;
  category?: string | null;
  note?: string;
  payload?: Record<string, unknown>;
  extra?: Record<string, unknown>;
}

interface AnnotationCreateResponse {
  ok: boolean;
  annotation_id: string;
}

export async function createAnnotation(
  req: AnnotationCreateRequest,
): Promise<AnnotationCreateResponse> {
  const res = await fetch(`${API_BASE}/api/annotations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      note: "",
      payload: {},
      extra: {},
      ...req,
    }),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`annotation create failed: ${res.status} ${detail}`);
  }
  return res.json();
}

export async function listPageAnnotations(
  pageId: string,
): Promise<AnnotationEvent[]> {
  const res = await fetch(`${API_BASE}/api/pages/${pageId}/annotations`);
  if (!res.ok) return [];
  return res.json();
}

// Batched fetch — issues one request per page but in parallel. There is no
// batch endpoint today; keep this in one place so we can swap in a single
// /api/projects/<id>/annotations call later without touching consumers.
export async function listPageAnnotationsBatch(
  pageIds: readonly string[],
): Promise<Map<string, AnnotationEvent[]>> {
  const entries = await Promise.all(
    pageIds.map(async (id) => {
      try {
        const rows = await listPageAnnotations(id);
        return [id, rows] as const;
      } catch {
        return [id, [] as AnnotationEvent[]] as const;
      }
    }),
  );
  return new Map(entries);
}
