"use client";

import { useEffect, useRef, useState } from "react";
import { fetchAppConfig, flagViewItem, unflagViewItem } from "@/lib/api";

// Minimal flag affordance that posts to /api/view-items/{id}/flag. Designed
// to sit in the same tight meta-row as PageAnnotationActions — a single
// instrumentation-flavored button (⚑) that on click runs a two-step
// prompt (category → message) and then briefly offers an inline "undo"
// before settling into a disabled "flagged" state.
//
// The button hides itself when the backend reports enable_flag_issue=false.
// Config is fetched once per tab and memoised via module state so every
// mounted button doesn't trigger its own HTTP call.

const CATEGORIES: Array<{ value: string; label: string }> = [
  { value: "problem", label: "problem — something's wrong here" },
  { value: "improvement", label: "improvement — this could be sharper" },
  { value: "factually_wrong", label: "factually wrong" },
  { value: "missing_consideration", label: "missing consideration" },
  { value: "reasoning_flawed", label: "reasoning flawed" },
  { value: "scope_confused", label: "scope confused" },
  { value: "other", label: "other" },
];

const UNDO_WINDOW_MS = 6000;

// Cache the app config once per tab.
let configPromise: Promise<boolean> | null = null;
function loadEnableFlagIssue(): Promise<boolean> {
  if (configPromise) return configPromise;
  configPromise = fetchAppConfig()
    .then((cfg) => cfg.enable_flag_issue)
    .catch(() => false);
  return configPromise;
}

interface Props {
  pageId: string;
}

type Phase =
  | { kind: "idle" }
  | { kind: "submitting" }
  | { kind: "undoable"; flagId: string }
  | { kind: "flagged" }
  | { kind: "undoing" };

export function ViewItemFlagButton({ pageId }: Props) {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const undoTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let cancelled = false;
    loadEnableFlagIssue().then((v) => {
      if (!cancelled) setEnabled(v);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // Reset local phase when the button is moved to a different page — a
  // drawer user can inspect → close → inspect another, and we don't want
  // the previous "flagged" state to leak.
  useEffect(() => {
    setPhase({ kind: "idle" });
    if (undoTimer.current) {
      clearTimeout(undoTimer.current);
      undoTimer.current = null;
    }
  }, [pageId]);

  useEffect(() => {
    return () => {
      if (undoTimer.current) clearTimeout(undoTimer.current);
    };
  }, []);

  if (enabled === null) return null;
  if (!enabled) return null;

  async function submit() {
    // Two-step prompt keeps this lightweight. A future iteration can swap
    // in a proper inline popover without changing the network contract.
    const categoryPrompt =
      "Flag category (type one):\n" +
      CATEGORIES.map((c, i) => `${i + 1}. ${c.label}`).join("\n") +
      "\n\nPick 1-7:";
    const raw = window.prompt(categoryPrompt, "1");
    if (raw === null) return;
    const idx = Number.parseInt(raw.trim(), 10);
    if (!Number.isFinite(idx) || idx < 1 || idx > CATEGORIES.length) {
      window.alert(`Enter a number 1–${CATEGORIES.length}.`);
      return;
    }
    const category = CATEGORIES[idx - 1].value;

    const message = window.prompt(
      `Flag as "${category}". What's wrong? (required)`,
      "",
    );
    if (message === null) return;
    const trimmed = message.trim();
    if (trimmed.length === 0) {
      window.alert("A flag message is required.");
      return;
    }

    setPhase({ kind: "submitting" });
    try {
      const { flag_id } = await flagViewItem(pageId, {
        category,
        message: trimmed,
      });
      setPhase({ kind: "undoable", flagId: flag_id });
      if (undoTimer.current) clearTimeout(undoTimer.current);
      undoTimer.current = setTimeout(() => {
        setPhase({ kind: "flagged" });
        undoTimer.current = null;
      }, UNDO_WINDOW_MS);
    } catch (e) {
      setPhase({ kind: "idle" });
      window.alert(
        e instanceof Error ? `failed: ${e.message}` : "failed to flag",
      );
    }
  }

  async function undo() {
    if (phase.kind !== "undoable") return;
    if (undoTimer.current) {
      clearTimeout(undoTimer.current);
      undoTimer.current = null;
    }
    const flagId = phase.flagId;
    setPhase({ kind: "undoing" });
    try {
      await unflagViewItem(flagId);
      setPhase({ kind: "idle" });
    } catch (e) {
      setPhase({ kind: "flagged" });
      window.alert(
        e instanceof Error ? `undo failed: ${e.message}` : "undo failed",
      );
    }
  }

  if (phase.kind === "undoable" || phase.kind === "undoing") {
    return (
      <button
        type="button"
        className="page-ann-btn page-ann-dispute"
        title="Undo flag (within grace window)"
        onClick={undo}
        disabled={phase.kind === "undoing"}
      >
        <span aria-hidden>↶</span>
        <span style={{ marginLeft: 4, fontSize: 9 }}>undo</span>
      </button>
    );
  }

  if (phase.kind === "flagged") {
    return (
      <button
        type="button"
        className="page-ann-btn page-ann-dispute"
        title="You flagged this page"
        disabled
      >
        <span aria-hidden>⚑</span>
      </button>
    );
  }

  return (
    <button
      type="button"
      className="page-ann-btn page-ann-dispute"
      title="Flag this view-item for issue"
      disabled={phase.kind === "submitting"}
      onClick={submit}
    >
      <span aria-hidden>⚑</span>
    </button>
  );
}
