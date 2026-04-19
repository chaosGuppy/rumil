// Metadata driving the formatted rendering of tool calls in TraceView.
// Keeps the known-keys table, short-id regex, and family-classification in
// one small module so TraceView.tsx stays focused on layout.

// 8-char lowercase hex short IDs, matched at word boundaries. Deliberately
// strict: matches bare refs like `abc12345` and bracketed refs like
// `[abc12345]` (brackets are non-word characters). Kept separate from
// NodeRefLink.NODE_ID_RE so regex state (global flag) doesn't get shared
// across independent consumers.
export const SHORT_ID_RE = /^[0-9a-f]{8}$/;

// Some arg values arrive as full UUIDs in tool calls (e.g. dispatch sends
// full IDs for `context_page_ids`). Treat the 8-char prefix as the short
// id for ref-rendering purposes.
export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export type KeyRenderHint =
  | "ref" // clickable short-id, opens inspect drawer (shift-click → pane)
  | "longtext" // preserved-whitespace body (content, headline, etc.)
  | "muted" // small, dim text (section, role, link_type)
  | "pill" // small inline pill (credence, robustness, importance)
  | "default"; // labeled row with JSON stringify

// Keys are matched exactly. Anything not in the table falls through to
// "default". The hint table is a hint, not a hard contract — long strings
// still get the longtext treatment regardless of key.
export const KEY_HINTS: Record<string, KeyRenderHint> = {
  page_id: "ref",
  target_page_id: "ref",
  scope_page_id: "ref",
  parent_id: "ref",
  from_page_id: "ref",
  to_page_id: "ref",
  view_id: "ref",
  question_id: "ref",
  artifact_id: "ref",
  source_page_id: "ref",
  consideration_id: "ref",
  judgement_id: "ref",

  headline: "longtext",
  content: "longtext",
  abstract: "longtext",
  reasoning: "longtext",
  rationale: "longtext",
  note: "longtext",

  credence: "pill",
  robustness: "pill",
  importance: "pill",

  section: "muted",
  role: "muted",
  link_type: "muted",
  direction: "muted",
};

// Priority order for building the one-line args preview. When the dict has
// multiple matching keys, the first one in this list wins. Keys not in
// this list may still appear in the preview if nothing higher-priority is
// present — we fall back to the first key in the dict.
export const PREVIEW_KEY_PRIORITY: readonly string[] = [
  "page_id",
  "target_page_id",
  "scope_page_id",
  "headline",
  "question_id",
  "view_id",
  "parent_id",
  "from_page_id",
  "to_page_id",
  "section",
  "role",
  "importance",
  "credence",
  "link_type",
];

// Family classification for the tool-name badge color. The palette reuses
// the existing --node-* CSS variables so the trace view stays visually
// coherent with page cards. Unmatched names fall through to "neutral".
export type ToolFamily =
  | "create"
  | "load"
  | "link"
  | "annotate"
  | "flag"
  | "update"
  | "remove"
  | "neutral";

export function classifyToolName(name: string): ToolFamily {
  const lower = name.toLowerCase();
  if (lower.startsWith("create_") || lower.startsWith("propose_")) return "create";
  if (lower.startsWith("load_") || lower.startsWith("explore_") || lower.startsWith("show_")) {
    return "load";
  }
  if (lower.startsWith("link_")) return "link";
  if (lower.startsWith("annotate_")) return "annotate";
  if (lower.startsWith("flag_") || lower.startsWith("report_")) return "flag";
  if (lower.startsWith("update_") || lower.startsWith("change_")) return "update";
  if (lower.startsWith("remove_") || lower.startsWith("delete_")) return "remove";
  return "neutral";
}

// Pick the "most interesting" key in a dict to surface in the one-line
// preview. Returns null for empty dicts. Prefers the priority list; if no
// priority key is present, returns the first key in insertion order.
export function pickPreviewKey(input: Record<string, unknown>): string | null {
  const keys = Object.keys(input);
  if (keys.length === 0) return null;
  for (const k of PREVIEW_KEY_PRIORITY) {
    if (k in input) return k;
  }
  return keys[0];
}

// Format a value for the inline preview: strings get quoted + truncated,
// short IDs surface as-is, objects collapse to `{...}`. Capped at ~60
// chars so the preview stays single-line with the key label.
export function formatPreviewValue(value: unknown): string {
  if (value === null) return "null";
  if (value === undefined) return "undefined";
  if (typeof value === "string") {
    if (SHORT_ID_RE.test(value) || UUID_RE.test(value)) {
      return shortenId(value);
    }
    const trimmed = value.replace(/\s+/g, " ").trim();
    if (trimmed.length <= 60) return `"${trimmed}"`;
    return `"${trimmed.slice(0, 57)}\u2026"`;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (Array.isArray(value)) {
    return value.length === 0 ? "[]" : `[${value.length}]`;
  }
  if (typeof value === "object") {
    const keys = Object.keys(value as object);
    return keys.length === 0 ? "{}" : `{${keys.length}}`;
  }
  return String(value);
}

// Truncate a UUID (or anything longer) to its 8-char prefix. Returns the
// input unchanged if already short.
export function shortenId(id: string): string {
  return id.length <= 8 ? id : id.slice(0, 8);
}

// Classify a string value. Returns "ref" for 8-char short IDs and full
// UUIDs; "longtext" for strings with newlines or length > 200; "default"
// otherwise. The renderer uses this alongside the key hint — e.g. a
// headline that's short still gets the longtext treatment by key, and a
// random long value gets longtext by content.
export function classifyValue(value: unknown): "ref" | "longtext" | "default" {
  if (typeof value !== "string") return "default";
  if (SHORT_ID_RE.test(value) || UUID_RE.test(value)) return "ref";
  if (value.length > 200 || value.includes("\n")) return "longtext";
  return "default";
}
