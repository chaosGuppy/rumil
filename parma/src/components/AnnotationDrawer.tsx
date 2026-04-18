"use client";

import { useEffect } from "react";
import type { AnnotationEvent } from "@/lib/annotations";

// Drawer for viewing the annotations attached to a page. Rendered next to
// the page body — not a global panel. Shows span annotations (with the
// anchored quote pulled from page text at render time) and page-level
// annotations.

interface AnnotationDrawerProps {
  pageId: string;
  pageText: string;
  annotations: AnnotationEvent[];
  onClose: () => void;
}

export function AnnotationDrawer({
  pageText,
  annotations,
  onClose,
}: AnnotationDrawerProps) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const pageLevel = annotations.filter((a) => a.span_start === null);
  const spans = annotations.filter((a) => a.span_start !== null);

  return (
    <aside className="ann-drawer" role="dialog" aria-label="Annotations">
      <header className="ann-drawer-head">
        <span className="ann-drawer-title">
          Annotations · {annotations.length}
        </span>
        <button
          type="button"
          className="ann-drawer-close"
          onClick={onClose}
          aria-label="Close"
        >
          close
        </button>
      </header>

      {annotations.length === 0 && (
        <div className="ann-drawer-empty">
          no annotations yet. select text in the page body to add one.
        </div>
      )}

      {pageLevel.length > 0 && (
        <section className="ann-drawer-section">
          <div className="ann-drawer-section-label">page-level</div>
          <ul className="ann-drawer-list">
            {pageLevel.map((a) => (
              <AnnotationRow key={a.id} ann={a} pageText={pageText} />
            ))}
          </ul>
        </section>
      )}

      {spans.length > 0 && (
        <section className="ann-drawer-section">
          <div className="ann-drawer-section-label">anchored to span</div>
          <ul className="ann-drawer-list">
            {spans.map((a) => (
              <AnnotationRow key={a.id} ann={a} pageText={pageText} />
            ))}
          </ul>
        </section>
      )}
    </aside>
  );
}

export function AnnotationRow({
  ann,
  pageText,
}: {
  ann: AnnotationEvent;
  pageText: string;
}) {
  const quote =
    ann.span_start !== null && ann.span_end !== null
      ? pageText.slice(ann.span_start, ann.span_end)
      : null;
  const label = categoryLabel(ann);
  const cls = categoryClass(ann);

  return (
    <li className={`ann-row ${cls}`}>
      <div className="ann-row-head">
        <span className={`ann-row-badge ${cls}`}>{label}</span>
        <span className="ann-row-author">
          {ann.author_type === "human" ? "human" : "model"}
        </span>
        <span className="ann-row-ts">
          {new Date(ann.created_at).toLocaleString("en-US", {
            month: "short",
            day: "numeric",
            hour: "numeric",
            minute: "2-digit",
          })}
        </span>
      </div>
      {quote && <blockquote className="ann-row-quote">{quote}</blockquote>}
      {ann.note && <div className="ann-row-note">{ann.note}</div>}
      {ann.annotation_type === "counterfactual_tool_use" &&
        Boolean(ann.payload?.alternative) && (
          <div className="ann-row-cf">
            would have used:{" "}
            <code>{String(ann.payload.alternative)}</code>
          </div>
        )}
    </li>
  );
}

function categoryLabel(a: AnnotationEvent): string {
  if (a.annotation_type === "counterfactual_tool_use") return "↯ counterfactual";
  switch (a.category) {
    case "endorsement":
      return "★ endorse";
    case "dispute":
      return "⚠ dispute";
    case "comment":
      return "¶ comment";
    case "factual_error":
      return "⚠ factual error";
    case "missing_consideration":
      return "◇ missing consideration";
    default:
      return a.category ?? a.annotation_type;
  }
}

function categoryClass(a: AnnotationEvent): string {
  if (a.annotation_type === "counterfactual_tool_use") return "ann-row-cf-kind";
  switch (a.category) {
    case "endorsement":
      return "ann-row-endorse";
    case "dispute":
    case "factual_error":
      return "ann-row-dispute";
    case "comment":
      return "ann-row-comment";
    default:
      return "ann-row-generic";
  }
}
