"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ViewShape, ViewItemShape } from "./page";
import "./read.css";

type QuickCategory =
  | "factually_wrong"
  | "missing_consideration"
  | "reasoning_flawed"
  | "scope_confused"
  | "other";

type FlagDialogState = {
  itemId: string;
  headline: string;
  category: QuickCategory;
  message: string;
  suggestedFix: string;
  submitting: boolean;
  error: string | null;
};

type FlaggedState = {
  flagId: string;
  expiresAt: number; // epoch ms — undo allowed until then
};

const QUICK_CATEGORIES: { value: QuickCategory; label: string }[] = [
  { value: "factually_wrong", label: "Claim is factually wrong" },
  { value: "missing_consideration", label: "Missing important consideration" },
  { value: "reasoning_flawed", label: "Reasoning doesn't follow" },
  { value: "scope_confused", label: "Scope is confused" },
  { value: "other", label: "Other" },
];

const UNDO_WINDOW_MS = 10_000;
const READ_DWELL_MS = 2_000;
const WELCOME_DISMISS_KEY = "rumil.read.welcome.dismissed.v1";
const READ_SESSION_KEY = "rumil.read.session.sent.v1";

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

function loadSessionSentIds(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = window.sessionStorage.getItem(READ_SESSION_KEY);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return new Set(Array.isArray(arr) ? arr : []);
  } catch {
    return new Set();
  }
}

function persistSessionSentIds(ids: Set<string>) {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(
      READ_SESSION_KEY,
      JSON.stringify(Array.from(ids)),
    );
  } catch {
    // Best-effort only — private-mode etc.
  }
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
  const [flagged, setFlagged] = useState<Map<string, FlaggedState>>(new Map());
  const [dialog, setDialog] = useState<FlagDialogState | null>(null);
  const [, forceTick] = useState(0);
  const [welcomeDismissed, setWelcomeDismissed] = useState(false);

  useEffect(() => {
    try {
      setWelcomeDismissed(
        window.localStorage.getItem(WELCOME_DISMISS_KEY) === "1",
      );
    } catch {
      setWelcomeDismissed(false);
    }
  }, []);

  // Tick every 500ms while any flag is inside its undo window so the countdown
  // rerenders live. Stops when no undo is pending.
  useEffect(() => {
    const anyPending = Array.from(flagged.values()).some(
      (f) => Date.now() < f.expiresAt,
    );
    if (!anyPending) return;
    const id = window.setInterval(() => forceTick((t) => t + 1), 500);
    return () => window.clearInterval(id);
  }, [flagged]);

  // --- read telemetry (IntersectionObserver + 2s dwell, dedup per session) ---
  const sentReadIdsRef = useRef<Set<string>>(new Set());
  const dwellTimersRef = useRef<Map<string, { startedAt: number; timeout: number }>>(
    new Map(),
  );

  useEffect(() => {
    sentReadIdsRef.current = loadSessionSentIds();
  }, []);

  const sendReadEvent = useCallback(
    async (itemId: string, seconds: number) => {
      if (sentReadIdsRef.current.has(itemId)) return;
      sentReadIdsRef.current.add(itemId);
      persistSessionSentIds(sentReadIdsRef.current);
      try {
        await fetch(`${apiBase}/api/view-items/${itemId}/read`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ seconds }),
        });
      } catch {
        // Silently ignore telemetry failures — never block the read UI.
      }
    },
    [apiBase],
  );

  const observerRef = useRef<IntersectionObserver | null>(null);

  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const id = (entry.target as HTMLElement).dataset.itemId;
          if (!id) continue;
          const timers = dwellTimersRef.current;
          if (entry.isIntersecting) {
            if (timers.has(id)) continue;
            if (sentReadIdsRef.current.has(id)) continue;
            const startedAt = Date.now();
            const timeout = window.setTimeout(() => {
              void sendReadEvent(id, (Date.now() - startedAt) / 1000);
              timers.delete(id);
            }, READ_DWELL_MS);
            timers.set(id, { startedAt, timeout });
          } else {
            const t = timers.get(id);
            if (t) {
              window.clearTimeout(t.timeout);
              timers.delete(id);
            }
          }
        }
      },
      { threshold: 0.5 },
    );
    observerRef.current = observer;
    const nodes = document.querySelectorAll("[data-item-id]");
    nodes.forEach((n) => observer.observe(n));
    return () => {
      observer.disconnect();
      dwellTimersRef.current.forEach((t) => window.clearTimeout(t.timeout));
      dwellTimersRef.current.clear();
      observerRef.current = null;
    };
  }, [sendReadEvent, view]);

  const openFlag = (item: ViewItemShape) => {
    setDialog({
      itemId: item.page.id,
      headline: item.page.headline,
      category: "factually_wrong",
      message: "",
      suggestedFix: "",
      submitting: false,
      error: null,
    });
  };

  const closeDialog = () => setDialog(null);

  const submitFlag = async (e: React.FormEvent<HTMLFormElement>) => {
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
      const body = (await res.json()) as { flag_id?: string };
      const flagId = body.flag_id ?? "";
      setFlagged((prev) => {
        const next = new Map(prev);
        next.set(dialog.itemId, {
          flagId,
          expiresAt: Date.now() + UNDO_WINDOW_MS,
        });
        return next;
      });
      setDialog(null);
    } catch (err) {
      setDialog({
        ...dialog,
        submitting: false,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const undoFlag = async (itemId: string) => {
    const entry = flagged.get(itemId);
    if (!entry || !entry.flagId) return;
    if (Date.now() >= entry.expiresAt) return;
    try {
      await fetch(`${apiBase}/api/view-items/flags/${entry.flagId}`, {
        method: "DELETE",
      });
    } catch {
      // If the network call fails we still remove it locally so the user
      // isn't stuck; a real failure is rare and they can refresh.
    }
    setFlagged((prev) => {
      const next = new Map(prev);
      next.delete(itemId);
      return next;
    });
  };

  const dismissWelcome = () => {
    try {
      window.localStorage.setItem(WELCOME_DISMISS_KEY, "1");
    } catch {
      // ignore
    }
    setWelcomeDismissed(true);
  };

  const totalItems = view.sections.reduce(
    (acc, s) => acc + s.items.length,
    0,
  );

  return (
    <main className="read-main">
      <div className="read-container">
        {!welcomeDismissed && (
          <aside className="read-welcome" role="note">
            <p>
              You&rsquo;re looking at an experimental research workspace&rsquo;s
              view of this question. Items here are pulled from a research graph
              that machines build and humans curate. If something seems wrong,
              incomplete, or confused &mdash; click{" "}
              <strong>&#9873; flag</strong> on the item. That&rsquo;s exactly
              what this interface is for, and your flags feed back into how the
              research improves.
            </p>
            <button
              type="button"
              className="read-welcome-dismiss"
              onClick={dismissWelcome}
              aria-label="Dismiss welcome message"
              title="Dismiss"
            >
              &times;
            </button>
          </aside>
        )}

        <header className="read-header">
          <p className="read-eyebrow">QUESTION</p>
          <h1 className="read-title">{view.question.headline}</h1>
          <p className="read-meta">
            {totalItems} items across {view.sections.length} sections &middot;
            research depth {view.health.max_depth}
            {!flaggingEnabled && (
              <span className="read-meta-flag-disabled">
                {" "}
                &middot; flagging disabled
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
                const flag = flagged.get(item.page.id);
                const now = Date.now();
                const undoSecsLeft =
                  flag && flag.expiresAt > now
                    ? Math.ceil((flag.expiresAt - now) / 1000)
                    : 0;
                return (
                  <li
                    key={`${section.name}-${item.page.id}`}
                    className="read-item"
                    data-type={item.page.page_type}
                    data-item-id={item.page.id}
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
                      {flag ? (
                        <div className="read-flagged-block">
                          <span className="read-flagged-thanks">
                            thanks &mdash; noted
                          </span>
                          {undoSecsLeft > 0 ? (
                            <button
                              type="button"
                              className="read-undo-btn"
                              onClick={() => undoFlag(item.page.id)}
                            >
                              undo ({undoSecsLeft}s)
                            </button>
                          ) : (
                            <button
                              type="button"
                              className="read-undo-btn"
                              disabled
                            >
                              flag saved
                            </button>
                          )}
                        </div>
                      ) : flaggingEnabled ? (
                        <button
                          type="button"
                          className="read-flag-btn"
                          onClick={() => openFlag(item)}
                          aria-label={`Flag: ${item.page.headline}`}
                        >
                          <span aria-hidden>&#9873;</span> flag
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

            <div className="read-field">
              <span className="read-field-label">What kind of issue?</span>
              <div className="read-dialog-choices">
                {QUICK_CATEGORIES.map((c) => (
                  <button
                    key={c.value}
                    type="button"
                    className={
                      "read-choice" +
                      (dialog.category === c.value ? " selected" : "")
                    }
                    onClick={() =>
                      setDialog({ ...dialog, category: c.value })
                    }
                  >
                    {c.label}
                  </button>
                ))}
              </div>
            </div>

            <label className="read-field">
              <span className="read-field-label">
                {dialog.category === "other" ? (
                  <>
                    Describe the issue{" "}
                    <span className="read-field-optional">(required)</span>
                  </>
                ) : (
                  <>
                    Details{" "}
                    <span className="read-field-optional">
                      (optional but helpful)
                    </span>
                  </>
                )}
              </span>
              <textarea
                required={dialog.category === "other"}
                rows={4}
                value={dialog.message}
                onChange={(e) =>
                  setDialog({ ...dialog, message: e.target.value })
                }
                placeholder="What specifically seems wrong or missing?"
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
                disabled={
                  dialog.submitting ||
                  (dialog.category === "other" && !dialog.message.trim())
                }
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
