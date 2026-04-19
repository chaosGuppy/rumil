"use client";

import { useEffect, useState } from "react";
import type { Page } from "@/lib/types";
import { fetchInlaysForQuestion } from "@/lib/api";

export const INLAY_SELECTION_STORAGE_KEY = "parma:inlay:selection";

// Per-question Inlay selection lives in localStorage. Key shape is
// `parma:inlay:selection:<short_question_id>` — the short id keeps
// keys short and human-readable when debugging via devtools. Value
// is the full UUID of the selected inlay, or the sentinel "stock"
// to explicitly override a prior selection back to the stock view.
export const STOCK_SENTINEL = "stock";

export function inlayStorageKey(questionId: string): string {
  return `${INLAY_SELECTION_STORAGE_KEY}:${questionId.slice(0, 8)}`;
}

export function loadInlaySelection(questionId: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(inlayStorageKey(questionId));
  } catch {
    return null;
  }
}

export function saveInlaySelection(
  questionId: string,
  value: string | null,
): void {
  if (typeof window === "undefined") return;
  try {
    const key = inlayStorageKey(questionId);
    if (value == null) {
      window.localStorage.removeItem(key);
    } else {
      window.localStorage.setItem(key, value);
    }
    // Notify same-tab listeners. `storage` events only fire across
    // tabs; components in the same tab need this explicit nudge.
    window.dispatchEvent(
      new CustomEvent("rumil:inlay:selection", {
        detail: { questionId, value },
      }),
    );
  } catch {
    /* localStorage unavailable — fail silently. */
  }
}

interface InlaySelectorProps {
  questionId: string;
  onLoaded?: (
    inlays: Page[],
    selected: Page | null,
    selectionValue: string | null,
  ) => void;
}

// Tiny dropdown that fetches inlays for the current question and
// persists the user's selection to localStorage. Hidden entirely
// when the question has no inlays so we don't clutter the chrome.
export function InlaySelector({ questionId, onLoaded }: InlaySelectorProps) {
  const [inlays, setInlays] = useState<Page[] | null>(null);
  const [selection, setSelection] = useState<string | null>(null);

  // Load once per question change. Swallow errors — missing inlays
  // should degrade to "no selector visible", not a page-level error.
  useEffect(() => {
    let cancelled = false;
    setInlays(null);
    fetchInlaysForQuestion(questionId)
      .then((rows) => {
        if (cancelled) return;
        setInlays(rows);
        const stored = loadInlaySelection(questionId);
        setSelection(stored);
        if (onLoaded) {
          const pickFromStored = (value: string | null): Page | null => {
            if (!value || value === STOCK_SENTINEL) return null;
            return rows.find((p) => p.id === value) ?? null;
          };
          onLoaded(rows, pickFromStored(stored), stored);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setInlays([]);
          if (onLoaded) onLoaded([], null, null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [questionId, onLoaded]);

  if (!inlays || inlays.length === 0) return null;

  const choose = (value: string) => {
    const next = value === STOCK_SENTINEL ? STOCK_SENTINEL : value;
    setSelection(next);
    saveInlaySelection(questionId, next);
  };

  return (
    <label
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "6px",
        fontFamily: "var(--font-mono-stack)",
        fontSize: "11px",
        color: "var(--fg-muted)",
      }}
      title="Swap the stock content area for a model-authored inlay"
    >
      <span style={{ letterSpacing: "0.04em" }}>inlay</span>
      <select
        value={selection ?? STOCK_SENTINEL}
        onChange={(e) => choose(e.target.value)}
        style={{
          fontFamily: "var(--font-mono-stack)",
          fontSize: "11px",
          padding: "2px 6px",
          background: "var(--bg-pane)",
          color: "var(--fg)",
          border: "1px solid var(--border)",
          borderRadius: "4px",
        }}
      >
        <option value={STOCK_SENTINEL}>stock view</option>
        {inlays.map((p) => (
          <option key={p.id} value={p.id}>
            {p.headline.length > 48
              ? `${p.headline.slice(0, 48)}…`
              : p.headline}
          </option>
        ))}
      </select>
    </label>
  );
}
