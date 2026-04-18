"use client";

import { useState } from "react";
import { createAnnotation } from "@/lib/annotations";
import { useAnnotations, useRegisterPage } from "./AnnotationContext";

// Page-level affordances: a small star + warning pair that posts an
// annotation with no span — i.e. "this whole page". Also exposes a drawer
// toggle for the annotations already on the page.
//
// Designed to fit into an existing meta-row alongside CredenceBadge,
// NodeTypeLabel, etc. The buttons are deliberately tiny (monospace 10px)
// to read as epistemic instrumentation, not CTA buttons.

interface PageAnnotationActionsProps {
  pageId: string;
  onOpenDrawer?: () => void;
}

export function PageAnnotationActions({
  pageId,
  onOpenDrawer,
}: PageAnnotationActionsProps) {
  const { annotationsForPage, invalidate } = useAnnotations();
  useRegisterPage(pageId);
  const [busy, setBusy] = useState<"endorse" | "dispute" | null>(null);
  const annotations = annotationsForPage(pageId);
  const pageLevel = annotations.filter((a) => a.span_start === null);
  const count = pageLevel.length;

  async function submit(
    category: "endorsement" | "dispute",
    annotationType: "endorsement" | "flag",
    promptText: string,
  ) {
    const note = window.prompt(promptText, "");
    if (note === null) return;
    setBusy(category === "endorsement" ? "endorse" : "dispute");
    try {
      await createAnnotation({
        annotation_type: annotationType,
        target_page_id: pageId,
        category,
        note: note.trim(),
      });
      invalidate(pageId);
    } catch (e) {
      window.alert(
        e instanceof Error ? `failed: ${e.message}` : "failed to record",
      );
    } finally {
      setBusy(null);
    }
  }

  return (
    <span className="page-ann-actions" aria-label="Page-level annotations">
      <button
        type="button"
        className="page-ann-btn page-ann-endorse"
        title="Endorse this page"
        disabled={busy !== null}
        onClick={() =>
          submit(
            "endorsement",
            "endorsement",
            "Endorse this page — why does it ring true? (optional)",
          )
        }
      >
        <span aria-hidden>★</span>
      </button>
      <button
        type="button"
        className="page-ann-btn page-ann-dispute"
        title="Dispute this page"
        disabled={busy !== null}
        onClick={() =>
          submit(
            "dispute",
            "flag",
            "Dispute this page — what's off? (required)",
          )
        }
      >
        <span aria-hidden>⚠</span>
      </button>
      {count > 0 && onOpenDrawer && (
        <button
          type="button"
          className="page-ann-count"
          title={`${count} page-level annotation${count === 1 ? "" : "s"}`}
          onClick={onOpenDrawer}
        >
          <span aria-hidden>📎</span>
          {count}
        </button>
      )}
    </span>
  );
}
