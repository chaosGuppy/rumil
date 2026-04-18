"use client";

import { useState, type FormEvent } from "react";
import type { ViewShape, ViewItemShape } from "./page";
import "./read.css";

type FlagCategory = "problem" | "improvement";

type FlagDialogState = {
  itemId: string;
  headline: string;
  category: FlagCategory;
  message: string;
  suggestedFix: string;
  submitting: boolean;
  error: string | null;
};

function readableSectionName(name: string): string {
  return name
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

function epistemicBadges(page: ViewItemShape["page"]): string {
  const parts: string[] = [];
  if (page.credence !== null && page.credence !== undefined) {
    parts.push(`C${page.credence}`);
  }
  if (page.robustness !== null && page.robustness !== undefined) {
    parts.push(`R${page.robustness}`);
  }
  if (page.importance !== null && page.importance !== undefined) {
    parts.push(`L${page.importance}`);
  }
  return parts.join("/");
}

export function ReadView({
  view,
  flaggingEnabled,
  apiBase,
}: {
  view: ViewShape;
  flaggingEnabled: boolean;
  apiBase: string;
}) {
  const [flagged, setFlagged] = useState<Set<string>>(new Set());
  const [dialog, setDialog] = useState<FlagDialogState | null>(null);

  const openFlag = (item: ViewItemShape) => {
    setDialog({
      itemId: item.page.id,
      headline: item.page.headline,
      category: "problem",
      message: "",
      suggestedFix: "",
      submitting: false,
      error: null,
    });
  };

  const closeDialog = () => setDialog(null);

  const submitFlag = async (e: FormEvent) => {
    e.preventDefault();
    if (!dialog) return;
    setDialog({ ...dialog, submitting: true, error: null });
    try {
      const res = await fetch(
        `${apiBase}/api/view-items/${dialog.itemId}/flag`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            category: dialog.category,
            message: dialog.message,
            suggested_fix: dialog.suggestedFix,
          }),
        },
      );
      if (!res.ok) {
        const body = await res.text();
        throw new Error(`${res.status}: ${body.slice(0, 200)}`);
      }
      setFlagged((prev) => new Set(prev).add(dialog.itemId));
      setDialog(null);
    } catch (err) {
      setDialog({
        ...dialog,
        submitting: false,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const totalItems = view.sections.reduce(
    (acc, s) => acc + s.items.length,
    0,
  );

  return (
    <main className="read-main">
      <div className="read-container">
        <header className="read-header">
          <p className="read-eyebrow">QUESTION</p>
          <h1 className="read-title">{view.question.headline}</h1>
          <p className="read-meta">
            {totalItems} items across {view.sections.length} sections ·
            research depth {view.health.max_depth}
            {!flaggingEnabled && (
              <span className="read-meta-flag-disabled">
                {" "}
                · flagging disabled
              </span>
            )}
          </p>
        </header>

        {view.sections.map((section) => (
          <section key={section.name} className="read-section">
            <h2 className="read-section-title">
              {readableSectionName(section.name)}
            </h2>
            <p className="read-section-desc">{section.description}</p>
            <ul className="read-items">
              {section.items.map((item) => {
                const badges = epistemicBadges(item.page);
                const isFlagged = flagged.has(item.page.id);
                return (
                  <li
                    key={`${section.name}-${item.page.id}`}
                    className="read-item"
                    data-type={item.page.page_type}
                  >
                    <div className="read-item-body">
                      <div className="read-item-row">
                        <span className="read-item-type">
                          {item.page.page_type}
                        </span>
                        {badges && (
                          <span className="read-item-badges">{badges}</span>
                        )}
                        <span className="read-item-short-id">
                          {item.page.id.slice(0, 8)}
                        </span>
                      </div>
                      <h3 className="read-item-headline">
                        {item.page.headline}
                      </h3>
                      {item.page.abstract &&
                        item.page.abstract !== item.page.headline && (
                          <p className="read-item-abstract">
                            {item.page.abstract}
                          </p>
                        )}
                    </div>
                    <div className="read-item-actions">
                      {isFlagged ? (
                        <span className="read-flagged">flagged — thanks</span>
                      ) : flaggingEnabled ? (
                        <button
                          type="button"
                          className="read-flag-btn"
                          onClick={() => openFlag(item)}
                          aria-label={`Flag: ${item.page.headline}`}
                        >
                          <span aria-hidden>⚑</span> flag
                        </button>
                      ) : null}
                    </div>
                  </li>
                );
              })}
            </ul>
          </section>
        ))}
      </div>

      {dialog && (
        <div
          className="read-dialog-backdrop"
          role="dialog"
          aria-modal="true"
          onClick={(e) => {
            if (e.target === e.currentTarget) closeDialog();
          }}
        >
          <form className="read-dialog" onSubmit={submitFlag}>
            <p className="read-dialog-eyebrow">FLAG THIS ITEM</p>
            <h2 className="read-dialog-title">{dialog.headline}</h2>

            <label className="read-field">
              <span className="read-field-label">Category</span>
              <select
                value={dialog.category}
                onChange={(e) =>
                  setDialog({
                    ...dialog,
                    category: e.target.value as FlagCategory,
                  })
                }
              >
                <option value="problem">Problem</option>
                <option value="improvement">Improvement</option>
              </select>
            </label>

            <label className="read-field">
              <span className="read-field-label">What&rsquo;s the issue?</span>
              <textarea
                required
                rows={4}
                value={dialog.message}
                onChange={(e) =>
                  setDialog({ ...dialog, message: e.target.value })
                }
                placeholder="Be specific about what would help."
              />
            </label>

            <label className="read-field">
              <span className="read-field-label">
                Suggested fix{" "}
                <span className="read-field-optional">(optional)</span>
              </span>
              <textarea
                rows={2}
                value={dialog.suggestedFix}
                onChange={(e) =>
                  setDialog({ ...dialog, suggestedFix: e.target.value })
                }
                placeholder="How might this be addressed?"
              />
            </label>

            {dialog.error && (
              <p className="read-dialog-error">Error: {dialog.error}</p>
            )}

            <div className="read-dialog-actions">
              <button
                type="button"
                className="read-btn-ghost"
                onClick={closeDialog}
                disabled={dialog.submitting}
              >
                Cancel
              </button>
              <button
                type="submit"
                className="read-btn-primary"
                disabled={dialog.submitting || !dialog.message.trim()}
              >
                {dialog.submitting ? "Submitting..." : "Submit flag"}
              </button>
            </div>
          </form>
        </div>
      )}
    </main>
  );
}
