"use client";

import { useEffect, useRef, useState } from "react";
import { createAnnotation } from "@/lib/annotations";
import { useAnnotations } from "./AnnotationContext";

// Editorial-style floating toolbar that appears over a live text selection
// inside a page body. Three quick actions (endorse / dispute / comment) and
// a "would have used" escape hatch for counterfactual tool-use.
//
// Span anchoring: we use absolute char offsets into the page's raw
// `content` string. Drift risk is real — the model can supersede the page
// and invalidate offsets — but the substrate MVP explicitly picks offsets
// as v1 (see marketplace-thread/28). A future patch can swap these for
// text-anchored spans (prefix/suffix match) without changing the toolbar.

const TOOL_CHOICES: ReadonlyArray<{ value: string; label: string }> = [
  { value: "web_research", label: "web research" },
  { value: "assess", label: "assess" },
  { value: "find_considerations", label: "find considerations" },
  { value: "scout", label: "scout" },
  { value: "ingest", label: "ingest" },
  { value: "create_view", label: "create view" },
];

export interface ToolbarSelection {
  pageId: string;
  text: string;
  start: number;
  end: number;
  anchorRect: DOMRect;
}

type Mode = "idle" | "comment" | "dispute" | "endorse" | "counterfactual";

interface AnnotationToolbarProps {
  selection: ToolbarSelection | null;
  onClose: () => void;
}

export function AnnotationToolbar({
  selection,
  onClose,
}: AnnotationToolbarProps) {
  const [mode, setMode] = useState<Mode>("idle");
  const [note, setNote] = useState("");
  const [tool, setTool] = useState<string>("web_research");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const { invalidate } = useAnnotations();

  useEffect(() => {
    setMode("idle");
    setNote("");
    setError(null);
  }, [selection?.start, selection?.end, selection?.pageId]);

  useEffect(() => {
    if (!selection) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [selection, onClose]);

  // Close when clicking outside the toolbar — but allow interaction inside
  // the expanded form.
  useEffect(() => {
    if (!selection) return;
    function onDown(e: MouseEvent) {
      if (!rootRef.current) return;
      if (rootRef.current.contains(e.target as Node)) return;
      onClose();
    }
    // deferred a tick so the originating mouseup doesn't immediately close
    const id = setTimeout(
      () => document.addEventListener("mousedown", onDown),
      0,
    );
    return () => {
      clearTimeout(id);
      document.removeEventListener("mousedown", onDown);
    };
  }, [selection, onClose]);

  if (!selection) return null;

  const viewport = typeof window === "undefined" ? null : window;
  const scrollX = viewport?.scrollX ?? 0;
  const scrollY = viewport?.scrollY ?? 0;

  const top = selection.anchorRect.top + scrollY - 52;
  const left =
    selection.anchorRect.left +
    scrollX +
    selection.anchorRect.width / 2;

  async function submitSpan(category: "comment" | "dispute" | "endorsement") {
    if (!selection) return;
    setSubmitting(true);
    setError(null);
    try {
      await createAnnotation({
        annotation_type: "span",
        target_page_id: selection.pageId,
        span_start: selection.start,
        span_end: selection.end,
        category,
        note: note.trim(),
      });
      invalidate(selection.pageId);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "submit failed");
    } finally {
      setSubmitting(false);
    }
  }

  async function submitCounterfactual() {
    if (!selection) return;
    setSubmitting(true);
    setError(null);
    try {
      await createAnnotation({
        annotation_type: "counterfactual_tool_use",
        target_page_id: selection.pageId,
        span_start: selection.start,
        span_end: selection.end,
        category: "tool_choice",
        note: note.trim(),
        payload: {
          alternative: tool,
          rationale: note.trim(),
          scope: "page_creation",
        },
      });
      invalidate(selection.pageId);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "submit failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div
      ref={rootRef}
      className={`ann-toolbar ann-toolbar-${mode}`}
      style={{ top, left }}
      role="dialog"
      aria-label="Annotate selection"
      onMouseDown={(e) => e.stopPropagation()}
    >
      {mode === "idle" && (
        <div className="ann-toolbar-row">
          <button
            type="button"
            className="ann-toolbar-btn ann-toolbar-btn-endorse"
            onClick={() => setMode("endorse")}
            title="Endorse this span"
          >
            <span aria-hidden>★</span>
            endorse
          </button>
          <span className="ann-toolbar-sep" />
          <button
            type="button"
            className="ann-toolbar-btn ann-toolbar-btn-dispute"
            onClick={() => setMode("dispute")}
            title="Dispute this span"
          >
            <span aria-hidden>⚠</span>
            dispute
          </button>
          <span className="ann-toolbar-sep" />
          <button
            type="button"
            className="ann-toolbar-btn"
            onClick={() => setMode("comment")}
            title="Leave a comment"
          >
            <span aria-hidden>¶</span>
            comment
          </button>
          <span className="ann-toolbar-sep" />
          <button
            type="button"
            className="ann-toolbar-btn ann-toolbar-btn-cf"
            onClick={() => setMode("counterfactual")}
            title="I'd have used a different tool"
          >
            <span aria-hidden>↯</span>
            would have used…
          </button>
        </div>
      )}

      {(mode === "comment" || mode === "dispute" || mode === "endorse") && (
        <form
          className="ann-form"
          onSubmit={(e) => {
            e.preventDefault();
            const map = {
              comment: "comment",
              dispute: "dispute",
              endorse: "endorsement",
            } as const;
            void submitSpan(map[mode]);
          }}
        >
          <div className="ann-form-head">
            <span className={`ann-form-label ann-form-label-${mode}`}>
              {mode === "endorse"
                ? "★ endorse"
                : mode === "dispute"
                  ? "⚠ dispute"
                  : "¶ comment"}
            </span>
            <span className="ann-form-quote">
              “{truncate(selection.text, 90)}”
            </span>
          </div>
          <textarea
            className="ann-form-textarea"
            autoFocus
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder={
              mode === "endorse"
                ? "why does this ring true? (optional)"
                : mode === "dispute"
                  ? "what's wrong? evidence, counterexample, source…"
                  : "say more…"
            }
            rows={3}
          />
          <div className="ann-form-foot">
            {error && <span className="ann-form-error">{error}</span>}
            <button
              type="button"
              className="ann-form-cancel"
              onClick={() => setMode("idle")}
              disabled={submitting}
            >
              back
            </button>
            <button
              type="submit"
              className="ann-form-submit"
              disabled={submitting || (mode === "dispute" && !note.trim())}
            >
              {submitting ? "submitting…" : "record"}
            </button>
          </div>
        </form>
      )}

      {mode === "counterfactual" && (
        <form
          className="ann-form"
          onSubmit={(e) => {
            e.preventDefault();
            void submitCounterfactual();
          }}
        >
          <div className="ann-form-head">
            <span className="ann-form-label ann-form-label-cf">
              ↯ would have used…
            </span>
            <span className="ann-form-quote">
              “{truncate(selection.text, 80)}”
            </span>
          </div>
          <div className="ann-form-field">
            <label className="ann-form-field-label">alternative</label>
            <select
              className="ann-form-select"
              value={tool}
              onChange={(e) => setTool(e.target.value)}
            >
              {TOOL_CHOICES.map((c) => (
                <option key={c.value} value={c.value}>
                  {c.label}
                </option>
              ))}
            </select>
          </div>
          <textarea
            className="ann-form-textarea"
            autoFocus
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="why would this tool have been a better fit?"
            rows={3}
          />
          <div className="ann-form-foot">
            {error && <span className="ann-form-error">{error}</span>}
            <button
              type="button"
              className="ann-form-cancel"
              onClick={() => setMode("idle")}
              disabled={submitting}
            >
              back
            </button>
            <button
              type="submit"
              className="ann-form-submit"
              disabled={submitting || !note.trim()}
            >
              {submitting ? "submitting…" : "record"}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

function truncate(s: string, n: number): string {
  const trimmed = s.replace(/\s+/g, " ").trim();
  return trimmed.length > n ? trimmed.slice(0, n - 1) + "…" : trimmed;
}
