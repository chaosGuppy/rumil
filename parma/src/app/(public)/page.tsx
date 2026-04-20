"use client";

import { useState, useCallback, useEffect, useMemo, useRef } from "react";
import { useSearchParams, useRouter, usePathname } from "next/navigation";
import { Suspense } from "react";
import { StackedPanes } from "@/components/StackedPanes";
import { ArticleView } from "@/components/ArticleView";
import { VerticalView } from "@/components/VerticalView";
import type { VerticalViewHandle } from "@/components/VerticalView";
import { ChatPanel } from "@/components/ChatPanel";
import { SuggestionReview } from "@/components/SuggestionReview";
import { SourcesView } from "@/components/SourcesView";
import { SectionsView } from "@/components/SectionsView";
import { SourceDrawer } from "@/components/SourceDrawer";
import { InspectPanel } from "@/components/InspectPanel";
import { InlayFrame } from "@/components/InlayFrame";
import {
  InlaySelector,
  STOCK_SENTINEL,
  inlayStorageKey,
} from "@/components/InlaySelector";
import {
  SearchPalette,
  useSearchPaletteShortcut,
} from "@/components/SearchPalette";
import { TraceView } from "@/components/TraceView";
import { ConceptProvider } from "@/components/ConceptContext";
import { AnnotationProvider } from "@/components/AnnotationContext";
import {
  InspectPanelProvider,
  useInspectPanel,
} from "@/components/InspectPanelContext";
import {
  continueResearch,
  createProject,
  createRootQuestion,
  fetchProjects,
  fetchProjectsSummary,
  fetchRootQuestions,
  fetchQuestionView,
  updateProject,
} from "@/lib/api";
import type { NavigateDirective } from "@/lib/api";
import { useDocumentTitle } from "@/lib/useDocumentTitle";
import type { QuestionView, Page, Project, ProjectSummary } from "@/lib/types";

const TEST_PROJECT_PATTERN = /^(test|scratch|smoke|tmp|scratchpad|skyblue-scratch|test-scratch)([-_].*)?$/i;
const CHAT_PERSIST_PATTERN = /^chat-persist-/;

function isTestProject(name: string): boolean {
  return TEST_PROJECT_PATTERN.test(name);
}

function isChatPersistProject(name: string): boolean {
  return CHAT_PERSIST_PATTERN.test(name);
}

type SortMode = "newest" | "oldest" | "alpha";
const SORT_MODES: SortMode[] = ["newest", "oldest", "alpha"];

function sortProjects(rows: ProjectSummary[], mode: SortMode): ProjectSummary[] {
  const copy = [...rows];
  switch (mode) {
    case "newest":
      return copy.sort(
        (a, b) =>
          new Date(b.last_activity_at).getTime() -
          new Date(a.last_activity_at).getTime(),
      );
    case "oldest":
      return copy.sort(
        (a, b) =>
          new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
      );
    case "alpha":
      return copy.sort((a, b) =>
        a.name.localeCompare(b.name, undefined, { sensitivity: "base" }),
      );
  }
}

const SHOW_TEST_STORAGE_KEY = "parma:showTestProjects";
const SHOW_HIDDEN_STORAGE_KEY = "parma:showHiddenProjects";
const SORT_STORAGE_KEY = "parma:projectSort";

function loadShowTest(): boolean {
  if (typeof window === "undefined") return false;
  return window.localStorage.getItem(SHOW_TEST_STORAGE_KEY) === "1";
}

function loadShowHidden(): boolean {
  if (typeof window === "undefined") return false;
  return window.localStorage.getItem(SHOW_HIDDEN_STORAGE_KEY) === "1";
}

function loadSort(): SortMode {
  if (typeof window === "undefined") return "newest";
  const raw = window.localStorage.getItem(SORT_STORAGE_KEY);
  return SORT_MODES.includes(raw as SortMode) ? (raw as SortMode) : "newest";
}

function formatRelative(iso: string): string {
  const now = Date.now();
  const then = new Date(iso).getTime();
  const diffMs = now - then;
  if (diffMs < 0) return "just now";
  const mins = Math.floor(diffMs / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 14) return `${days}d ago`;
  const weeks = Math.floor(days / 7);
  if (weeks < 8) return `${weeks}w ago`;
  const months = Math.floor(days / 30);
  if (months < 18) return `${months}mo ago`;
  const years = Math.floor(days / 365);
  return `${years}y ago`;
}

const VIEW_MODES = ["panes", "article", "vertical", "sections", "sources", "trace"] as const;
type ViewMode = (typeof VIEW_MODES)[number];

function isViewMode(v: string): v is ViewMode {
  return (VIEW_MODES as readonly string[]).includes(v);
}

// InlineRename — shared click-to-edit text field used for workspace rename.
// - Idle: renders `children` wrapped in a span; clicking flips to edit mode.
// - Edit: focused text input; Enter commits via `onCommit`, Esc reverts.
// - A commit error (server 409, validation) is rendered inline below the
//   input; the field stays open so the user can tweak and retry.
//
// `onCommit` must return a promise. Resolution closes the editor; rejection
// stays open with the rejection message surfaced inline. `variant`
// controls visual density — "card" fits the landing card's 20px headline,
// "switcher" is the compact form used in the view switcher header.
function InlineRename({
  value,
  onCommit,
  variant,
  title,
  className,
  trigger = "click",
}: {
  value: string;
  onCommit: (next: string) => Promise<void>;
  variant: "card" | "switcher";
  title?: string;
  className?: string;
  // "click" = single click enters edit mode (switcher default). "dblclick" =
  // double-click enters edit mode and single clicks bubble up, so the
  // parent can treat the label as an open-the-thing affordance (card).
  trigger?: "click" | "dblclick";
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) {
      setDraft(value);
      setError(null);
      // Defer focus so the input is mounted before we grab it.
      requestAnimationFrame(() => {
        inputRef.current?.focus();
        inputRef.current?.select();
      });
    }
  }, [editing, value]);

  const cancel = useCallback(() => {
    setEditing(false);
    setDraft(value);
    setError(null);
  }, [value]);

  const submit = useCallback(async () => {
    const trimmed = draft.trim();
    if (!trimmed) {
      setError("Workspace name can't be empty.");
      return;
    }
    if (trimmed === value) {
      setEditing(false);
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onCommit(trimmed);
      setEditing(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not rename");
    } finally {
      setSubmitting(false);
    }
  }, [draft, value, onCommit]);

  if (!editing) {
    const useDblClick = trigger === "dblclick";
    return (
      <span
        className={`inline-rename inline-rename-${variant} ${className ?? ""}`}
        title={title ?? (useDblClick ? "Double-click to rename" : "Click to rename")}
        onClick={
          useDblClick
            ? undefined
            : (e) => {
                e.stopPropagation();
                setEditing(true);
              }
        }
        onDoubleClick={
          useDblClick
            ? (e) => {
                e.stopPropagation();
                setEditing(true);
              }
            : undefined
        }
        onKeyDown={(e) => {
          // Keyboard shortcut for rename lives on the card root, not here —
          // we don't want Enter on a focused name to both open the card (via
          // the card's own handler) and enter edit mode.
          if (!useDblClick && (e.key === "Enter" || e.key === " ")) {
            e.preventDefault();
            e.stopPropagation();
            setEditing(true);
          }
        }}
        role={useDblClick ? undefined : "textbox"}
        aria-label={
          useDblClick
            ? `${value} (double-click to rename)`
            : `${value} (click to rename)`
        }
        tabIndex={useDblClick ? -1 : 0}
      >
        {value}
      </span>
    );
  }

  return (
    <span
      className={`inline-rename inline-rename-editing inline-rename-${variant}`}
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => e.stopPropagation()}
    >
      <input
        ref={inputRef}
        type="text"
        className="inline-rename-input"
        value={draft}
        maxLength={80}
        disabled={submitting}
        onChange={(e) => {
          setDraft(e.target.value);
          if (error) setError(null);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            submit();
          } else if (e.key === "Escape") {
            e.preventDefault();
            cancel();
          }
        }}
        onBlur={() => {
          // Only cancel on blur if no submit is in flight and the draft is
          // unchanged — otherwise the user is mid-commit and we shouldn't
          // swallow their edit.
          if (!submitting && draft.trim() === value) {
            cancel();
          }
        }}
      />
      {error && <span className="inline-rename-error">{error}</span>}
    </span>
  );
}

function ViewModeSwitcher({
  current,
  onChange,
  extra,
  onBack,
  label,
  onRename,
}: {
  current: ViewMode;
  onChange: (mode: ViewMode) => void;
  extra?: React.ReactNode;
  onBack?: () => void;
  label?: string;
  onRename?: (next: string) => Promise<void>;
}) {
  return (
    <div className="view-switcher">
      <div className="view-switcher-row">
        {onBack && (
          <>
            <button
              className="view-switcher-back"
              onClick={onBack}
              title="Back"
            >
              Home
            </button>
            {label && (
              <span className="view-switcher-ws-name">
                {onRename ? (
                  <InlineRename
                    value={label}
                    onCommit={onRename}
                    variant="switcher"
                    title="Rename workspace"
                  />
                ) : (
                  label
                )}
              </span>
            )}
          </>
        )}
        {VIEW_MODES.map((mode) => (
          <button
            key={mode}
            className={`view-switcher-btn ${current === mode ? "active" : ""}`}
            onClick={() => onChange(mode)}
          >
            {mode}
          </button>
        ))}
        {extra}
      </div>
    </div>
  );
}

function NewWorkspaceModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (project: Project, created: boolean) => void;
}) {
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !submitting) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, submitting]);

  const submit = useCallback(async () => {
    const trimmed = name.trim();
    if (!trimmed || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const result = await createProject(trimmed);
      onCreated(result.project, result.created);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not create workspace");
      setSubmitting(false);
    }
  }, [name, submitting, onCreated]);

  const disabled = !name.trim() || submitting;

  return (
    <div
      className="workspace-modal-backdrop"
      onMouseDown={(e) => {
        // Only close if the mousedown started on the backdrop itself —
        // otherwise a drag-release from the input into the backdrop would
        // swallow the modal mid-edit.
        if (e.target === e.currentTarget && !submitting) onClose();
      }}
    >
      <div className="workspace-modal" role="dialog" aria-label="New workspace">
        <div className="workspace-modal-label">New workspace</div>
        <input
          ref={inputRef}
          className="workspace-modal-input"
          type="text"
          value={name}
          placeholder="workspace-name"
          maxLength={80}
          disabled={submitting}
          onChange={(e) => {
            setName(e.target.value);
            if (error) setError(null);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              submit();
            }
          }}
        />
        {error && <div className="workspace-modal-error">{error}</div>}
        <div className="workspace-modal-actions">
          <button
            type="button"
            className="workspace-modal-cancel"
            onClick={onClose}
            disabled={submitting}
          >
            Cancel
          </button>
          <button
            type="button"
            className="workspace-modal-submit"
            onClick={submit}
            disabled={disabled}
          >
            {submitting ? "Creating..." : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}

function ProjectBrowser({
  onSelectProject,
}: {
  onSelectProject: (project: Project) => void;
}) {
  const [rows, setRows] = useState<ProjectSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showTest, setShowTest] = useState(false);
  const [showHidden, setShowHidden] = useState(false);
  const [sort, setSort] = useState<SortMode>("newest");
  const [modalOpen, setModalOpen] = useState(false);
  const [collisionHint, setCollisionHint] = useState<string | null>(null);
  // Local optimistic state for per-card hide/unhide + rename. We mutate
  // `rows` in place on success so the grid reflects the change without a
  // full refetch.
  const [busyId, setBusyId] = useState<string | null>(null);

  useDocumentTitle(["projects"]);

  // Hydrate UI preferences from localStorage. Deferred to an effect so the
  // first render matches the server and we don't flash-unhydrate.
  useEffect(() => {
    setShowTest(loadShowTest());
    setShowHidden(loadShowHidden());
    setSort(loadSort());
  }, []);

  useEffect(() => {
    // Refetch whenever the show-hidden toggle flips — the backend decides
    // whether to include hidden rows so the summary stats stay authoritative
    // instead of living in two places.
    fetchProjectsSummary(showHidden)
      .then(setRows)
      .catch((e) => setError(e?.message ?? "failed"));
  }, [showHidden]);

  const persistShowTest = useCallback((next: boolean) => {
    setShowTest(next);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(SHOW_TEST_STORAGE_KEY, next ? "1" : "0");
    }
  }, []);

  const persistShowHidden = useCallback((next: boolean) => {
    setShowHidden(next);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(SHOW_HIDDEN_STORAGE_KEY, next ? "1" : "0");
    }
  }, []);

  const persistSort = useCallback((next: SortMode) => {
    setSort(next);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(SORT_STORAGE_KEY, next);
    }
  }, []);

  const handleToggleHidden = useCallback(
    async (project: ProjectSummary) => {
      if (busyId) return;
      setBusyId(project.id);
      try {
        const next = !project.hidden;
        const updated = await updateProject(project.id, { hidden: next });
        setRows((prev) => {
          if (!prev) return prev;
          // When hiding and the toggle is off, drop the row entirely so it
          // vanishes from the grid. When unhiding, keep it in place — the
          // user just unhid it, they probably want to still see it.
          if (next && !showHidden) {
            return prev.filter((r) => r.id !== project.id);
          }
          return prev.map((r) =>
            r.id === project.id ? { ...r, hidden: updated.hidden } : r,
          );
        });
      } catch (e) {
        setError(e instanceof Error ? e.message : "Could not update workspace");
      } finally {
        setBusyId(null);
      }
    },
    [busyId, showHidden],
  );

  // Inline rename from the card. Throws on server error (422/409) so the
  // InlineRename widget can render the message inline and stay open; on
  // success we swap the name in the cached rows so subsequent renders
  // don't fetch again.
  const handleRenameCard = useCallback(
    async (projectId: string, nextName: string) => {
      const updated = await updateProject(projectId, { name: nextName });
      setRows((prev) => {
        if (!prev) return prev;
        return prev.map((r) =>
          r.id === projectId ? { ...r, name: updated.name } : r,
        );
      });
    },
    [],
  );

  const filtered = useMemo(() => {
    if (!rows) return null;
    const withoutChatPersist = rows.filter((r) => !isChatPersistProject(r.name));
    const live = showTest
      ? withoutChatPersist
      : withoutChatPersist.filter((r) => !isTestProject(r.name));
    return sortProjects(live, sort);
  }, [rows, showTest, sort]);

  const hiddenTestCount = useMemo(() => {
    if (!rows) return 0;
    const withoutChatPersist = rows.filter((r) => !isChatPersistProject(r.name));
    return showTest ? 0 : withoutChatPersist.filter((r) => isTestProject(r.name)).length;
  }, [rows, showTest]);

  // Count of hidden rows currently in `rows` — only meaningful when
  // showHidden=true (otherwise the backend filters them out and the count
  // is always zero). Used to hint "(N)" next to the toggle when visible.
  const visibleHiddenCount = useMemo(() => {
    if (!rows) return 0;
    return rows.filter((r) => r.hidden).length;
  }, [rows]);

  if (!rows && !error) {
    return <div className="browser-loading">Loading projects...</div>;
  }

  if (error) {
    return (
      <div className="view-error">
        Could not load projects: {error}
        <br />
        Is the rumil API running? (./scripts/dev-api.sh)
      </div>
    );
  }

  const projects = filtered ?? [];

  return (
    <div className="landing">
      <header className="landing-header">
        <div className="landing-header-inner">
          <h1 className="landing-title">Projects</h1>
          <p className="landing-subtitle">
            An index of investigations. Each project is a living graph of
            questions, claims, and the calls that produced them.
          </p>
        </div>

        <div className="landing-controls">
          <div className="landing-sort" role="tablist" aria-label="Sort">
            {SORT_MODES.map((mode) => (
              <button
                key={mode}
                role="tab"
                aria-selected={sort === mode}
                className={`landing-sort-btn ${sort === mode ? "active" : ""}`}
                onClick={() => persistSort(mode)}
              >
                {mode}
              </button>
            ))}
          </div>

          <label className="landing-toggle">
            <input
              type="checkbox"
              checked={showTest}
              onChange={(e) => persistShowTest(e.target.checked)}
            />
            <span>
              show test projects
              {hiddenTestCount > 0 && (
                <em className="landing-toggle-hint">({hiddenTestCount} hidden)</em>
              )}
            </span>
          </label>

          <label className="landing-toggle">
            <input
              type="checkbox"
              checked={showHidden}
              onChange={(e) => persistShowHidden(e.target.checked)}
            />
            <span>
              show hidden
              {showHidden && visibleHiddenCount > 0 && (
                <em className="landing-toggle-hint">({visibleHiddenCount})</em>
              )}
            </span>
          </label>

          <button
            type="button"
            className="landing-new-btn"
            onClick={() => {
              setCollisionHint(null);
              setModalOpen(true);
            }}
          >
            + new workspace
          </button>
        </div>
        {collisionHint && (
          <div className="landing-hint" role="status">
            Workspace <code>{collisionHint}</code> already existed — showing it.
          </div>
        )}
      </header>

      {projects.length === 0 ? (
        <div className="landing-empty">
          {rows && rows.length > 0
            ? "All projects filtered out. Toggle 'show test projects' to reveal them."
            : "No projects found. Start the rumil API and create a workspace."}
        </div>
      ) : (
        <div className="landing-grid">
          {projects.map((p) => {
            const empty =
              p.question_count === 0 &&
              p.claim_count === 0 &&
              p.call_count === 0;
            const classes = [
              "landing-card",
              empty ? "is-empty" : "",
              p.hidden ? "is-hidden-project" : "",
              busyId === p.id ? "is-busy" : "",
            ]
              .filter(Boolean)
              .join(" ");
            const openProject = () =>
              onSelectProject({
                id: p.id,
                name: p.name,
                created_at: p.created_at,
                hidden: p.hidden,
              });
            return (
              <div
                key={p.id}
                role="button"
                tabIndex={0}
                className={classes}
                onClick={openProject}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    openProject();
                  }
                }}
              >
                <button
                  type="button"
                  className="landing-card-hide"
                  onClick={(e) => {
                    e.stopPropagation();
                    handleToggleHidden(p);
                  }}
                  title={p.hidden ? "Unhide workspace" : "Hide workspace"}
                  aria-label={p.hidden ? "Unhide workspace" : "Hide workspace"}
                  disabled={busyId === p.id}
                >
                  {p.hidden ? "unhide" : "hide"}
                </button>

                <div className="landing-card-top">
                  <div className="landing-card-name">
                    <InlineRename
                      value={p.name}
                      onCommit={(next) => handleRenameCard(p.id, next)}
                      variant="card"
                      title="Double-click to rename workspace"
                      trigger="dblclick"
                    />
                  </div>
                  <div className="landing-card-badges">
                    {p.hidden && (
                      <span className="landing-card-hidden-badge">hidden</span>
                    )}
                    {empty && (
                      <span className="landing-card-empty-badge">empty</span>
                    )}
                  </div>
                </div>

                <dl className="landing-card-stats">
                  <div className="landing-stat">
                    <dt title="Includes root questions and sub-questions">
                      questions (incl. sub)
                    </dt>
                    <dd>{p.question_count}</dd>
                  </div>
                  <div className="landing-stat">
                    <dt>claims</dt>
                    <dd>{p.claim_count}</dd>
                  </div>
                  <div className="landing-stat">
                    <dt>calls</dt>
                    <dd>{p.call_count}</dd>
                  </div>
                </dl>

                <div className="landing-card-foot">
                  <span>last activity {formatRelative(p.last_activity_at)}</span>
                </div>
              </div>
            );
          })}
        </div>
      )}
      {modalOpen && (
        <NewWorkspaceModal
          onClose={() => setModalOpen(false)}
          onCreated={(project, created) => {
            setModalOpen(false);
            setCollisionHint(created ? null : project.name);
            // Route into the new workspace using the same path as clicking
            // an existing card — this updates `selectedProject` directly
            // and kicks off the question fetch, rather than relying on
            // URL-param hydration (which doesn't fire reliably on a
            // same-route push).
            onSelectProject(project);
          }}
        />
      )}
    </div>
  );
}

// AskQuestionForm — inline form for creating a bare root question in the
// active workspace. No research is triggered; the user is redirected into
// the new question where they can start chatting to populate it.
//
// Used in two places:
//   1. As the primary affordance when a workspace has zero questions
//      (replaces the old "no questions found" dead-end).
//   2. As an expandable affordance inside QuestionPicker so users with
//      existing questions aren't stuck.
//
// `variant="empty"` renders with larger type and the subtitle; `variant="picker"`
// is more compact and sits under the picker header.
function AskQuestionForm({
  projectName,
  onCreated,
  variant,
  onCancel,
}: {
  projectName: string;
  onCreated: (question: Page) => void;
  variant: "empty" | "picker";
  onCancel?: () => void;
}) {
  const [headline, setHeadline] = useState("");
  const [content, setContent] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const headlineRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    headlineRef.current?.focus();
  }, []);

  const submit = useCallback(
    async (e?: React.FormEvent) => {
      e?.preventDefault();
      const trimmed = headline.trim();
      if (!trimmed || submitting) return;
      setSubmitting(true);
      setError(null);
      try {
        // projectName actually holds the project *id* when called from
        // AppContent (handleCreateQuestion below passes selectedProject.id)
        // — this component never needs to resolve names itself.
        const page = await createRootQuestion(
          projectName,
          trimmed,
          content.trim() || undefined,
        );
        onCreated(page);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not create question");
        setSubmitting(false);
      }
    },
    [headline, content, submitting, projectName, onCreated],
  );

  const disabled = !headline.trim() || submitting;

  return (
    <form
      className={`ask-form ask-form-${variant}`}
      onSubmit={submit}
      aria-label="Ask a question"
    >
      {variant === "empty" && (
        <div className="ask-form-lede">
          <div className="ask-form-lede-eyebrow">start here</div>
          <h2 className="ask-form-lede-title">Ask a question</h2>
          <p className="ask-form-lede-body">
            Seed this workspace with a root question. No research runs yet —
            once it exists you can use chat (<code>/orchestrate</code>,{" "}
            <code>/dispatch</code>, <code>/ask</code>) to investigate it.
          </p>
        </div>
      )}

      <label className="ask-form-field">
        <span className="ask-form-label">Headline</span>
        <input
          ref={headlineRef}
          className="ask-form-input"
          type="text"
          value={headline}
          placeholder="What do you want to know?"
          maxLength={300}
          disabled={submitting}
          onChange={(e) => {
            setHeadline(e.target.value);
            if (error) setError(null);
          }}
        />
      </label>

      <label className="ask-form-field">
        <span className="ask-form-label">
          Context <em className="ask-form-label-optional">optional</em>
        </span>
        <textarea
          className="ask-form-textarea"
          value={content}
          placeholder="Anything that frames the question. Leave blank to start with just the headline."
          rows={variant === "empty" ? 4 : 3}
          disabled={submitting}
          onChange={(e) => {
            setContent(e.target.value);
            if (error) setError(null);
          }}
        />
      </label>

      {error && <div className="ask-form-error">{error}</div>}

      <div className="ask-form-actions">
        {onCancel && (
          <button
            type="button"
            className="ask-form-cancel"
            onClick={onCancel}
            disabled={submitting}
          >
            Cancel
          </button>
        )}
        <button
          type="submit"
          className="ask-form-submit"
          disabled={disabled}
        >
          {submitting ? "Creating..." : "Create question"}
        </button>
      </div>
    </form>
  );
}

function QuestionPicker({
  project,
  questions,
  onSelect,
  onBack,
  onCreateQuestion,
}: {
  project: Project;
  questions: Page[];
  onSelect: (question: Page) => void;
  onBack: () => void;
  onCreateQuestion: (question: Page) => void;
}) {
  const [creating, setCreating] = useState(false);
  useDocumentTitle([project.name]);
  return (
    <div className="browser">
      <div className="browser-header">
        <button
          onClick={onBack}
          style={{
            background: "none",
            border: "none",
            cursor: "pointer",
            fontFamily: "var(--font-mono-stack)",
            fontSize: "11px",
            color: "var(--fg-dim)",
            padding: "0 0 8px 0",
            letterSpacing: "0.04em",
          }}
        >
          ← projects
        </button>
        <div className="browser-header-row">
          <div>
            <h1 className="browser-title">{project.name}</h1>
            <p className="browser-subtitle">
              {questions.length} root question{questions.length !== 1 ? "s" : ""}
            </p>
          </div>
          {!creating && (
            <button
              type="button"
              className="browser-new-btn"
              onClick={() => setCreating(true)}
            >
              + new question
            </button>
          )}
        </div>
      </div>

      {creating && (
        <AskQuestionForm
          projectName={project.id}
          variant="picker"
          onCancel={() => setCreating(false)}
          onCreated={onCreateQuestion}
        />
      )}

      <div className="browser-list">
        {questions.map((q) => {
          // Disambiguate same-titled root questions: show the short id plus a
          // relative timestamp (precise timestamp in the tooltip) so two
          // "wave7-smoke" style duplicates don't look identical in the picker.
          const createdAbsolute = new Date(q.created_at).toLocaleString("en-US", {
            year: "numeric",
            month: "short",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
          });
          const triage = (q.extra?.triage ?? null) as
            | { is_duplicate?: boolean; duplicate_of?: string | null }
            | null;
          const dupeOf =
            triage?.is_duplicate && typeof triage.duplicate_of === "string"
              ? triage.duplicate_of
              : null;
          return (
            <button
              key={q.id}
              className="browser-card"
              onClick={() => onSelect(q)}
            >
              <div className="browser-card-name">{q.headline}</div>
              <div className="browser-card-stats">
                <span className="browser-card-id">{q.id.slice(0, 8)}</span>
                {" · "}
                <span title={createdAbsolute}>
                  created {formatRelative(q.created_at)}
                </span>
                {dupeOf && (
                  <>
                    {" · "}
                    <span
                      className="browser-card-dupe-badge"
                      title={`Triage marked this as a duplicate of ${dupeOf}`}
                    >
                      duplicate of {dupeOf.slice(0, 8)}
                    </span>
                  </>
                )}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// Modal for kicking off a new orchestrator run on the current question.
// Default budget of 5 keeps smoke-test costs predictable; cap at 20 matches
// the CLI convention (anything bigger should be explicit). On submit we
// POST /continue, receive a run_id, and hand off to the parent via
// onLaunched — the parent navigates to the trace.
function ContinueResearchModal({
  questionId,
  onClose,
  onLaunched,
}: {
  questionId: string;
  onClose: () => void;
  onLaunched: (runId: string) => void;
}) {
  const [budget, setBudget] = useState<number>(5);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !submitting) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, submitting]);

  const submit = useCallback(async () => {
    if (submitting) return;
    if (!Number.isFinite(budget) || budget < 1 || budget > 20) {
      setError("Budget must be between 1 and 20.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const res = await continueResearch(questionId, budget);
      onLaunched(res.run_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start run");
      setSubmitting(false);
    }
  }, [budget, submitting, questionId, onLaunched]);

  return (
    <div
      className="workspace-modal-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !submitting) onClose();
      }}
    >
      <div className="workspace-modal" role="dialog" aria-label="Continue research">
        <div className="workspace-modal-label">Continue research</div>
        <div className="continue-modal-hint">
          Kick off an orchestrator run against this question. The trace will
          open so you can watch it happen.
        </div>
        <label className="continue-modal-field">
          <span className="continue-modal-field-label">budget</span>
          <input
            ref={inputRef}
            type="number"
            min={1}
            max={20}
            step={1}
            value={budget}
            disabled={submitting}
            onChange={(e) => {
              // Empty input → NaN; we keep the current value so the user
              // can clear + retype without the field snapping to 0.
              const n = e.target.value === "" ? NaN : Number(e.target.value);
              setBudget(n);
              if (error) setError(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                submit();
              }
            }}
            className="workspace-modal-input continue-modal-input"
          />
          <span className="continue-modal-field-hint">calls · max 20</span>
        </label>
        {error && <div className="workspace-modal-error">{error}</div>}
        <div className="workspace-modal-actions">
          <button
            type="button"
            className="workspace-modal-cancel"
            onClick={onClose}
            disabled={submitting}
          >
            Cancel
          </button>
          <button
            type="button"
            className="workspace-modal-submit"
            onClick={submit}
            disabled={submitting}
          >
            {submitting ? "Starting..." : "Start run"}
          </button>
        </div>
      </div>
    </div>
  );
}

function QuestionViewPage({
  project,
  questionId,
  onBack,
  onRenameProject,
}: {
  project: Project;
  questionId: string;
  onBack: () => void;
  onRenameProject?: (next: string) => Promise<void>;
}) {
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const {
    openInspect,
    closeInspect,
    openShortId,
    promoteToPane,
    registerTraceHandler,
  } = useInspectPanel();

  const verticalRef = useRef<VerticalViewHandle>(null);
  const [chatOpen, setChatOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useSearchPaletteShortcut();
  const toggleChat = useCallback(() => setChatOpen((v) => !v), []);
  const [view, setView] = useState<QuestionView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [focusNodeId, setFocusNodeId] = useState<string | null>(null);
  const [showReview, setShowReview] = useState(false);
  const [drawerSource, setDrawerSource] = useState<Page | null>(null);
  const [continueOpen, setContinueOpen] = useState(false);
  // Inlay selection: `null` = not yet loaded, `{inlays, selected}` once
  // the selector finishes its fetch. `selected` is the resolved Page
  // or null when the user picked "stock" / the stored id no longer
  // exists. Persists across view-mode switches but is cleared when the
  // question changes.
  const [inlayState, setInlayState] = useState<{
    inlays: Page[];
    selected: Page | null;
  } | null>(null);
  const handleInlayLoaded = useCallback(
    (inlays: Page[], selected: Page | null) => {
      setInlayState({ inlays, selected });
    },
    [],
  );
  // Listen for cross-component selection events so the selector and
  // InlayFrame stay in sync without prop drilling. Keyed per question
  // so opening a second tab with a different question doesn't cross
  // its wires.
  useEffect(() => {
    function onSelection(e: Event) {
      const detail = (e as CustomEvent).detail;
      if (!detail || detail.questionId !== questionId) return;
      if (typeof window === "undefined") return;
      const value = window.localStorage.getItem(inlayStorageKey(questionId));
      setInlayState((prev) => {
        if (!prev) return prev;
        if (!value || value === STOCK_SENTINEL) {
          return { ...prev, selected: null };
        }
        const match = prev.inlays.find((p) => p.id === value) ?? null;
        return { ...prev, selected: match };
      });
    }
    window.addEventListener("rumil:inlay:selection", onSelection);
    return () =>
      window.removeEventListener("rumil:inlay:selection", onSelection);
  }, [questionId]);

  // When a node ref is clicked in chat: open the inspect panel AND nudge the
  // view to scroll to the matching card if one is visible. The inspect
  // panel is the richer surface; focus-scroll is a nice-to-have.
  const handleNodeRef = useCallback(
    (id: string) => {
      openInspect(id);
      setFocusNodeId(id);
    },
    [openInspect],
  );

  const rawView = searchParams.get("view") ?? "panes";
  const viewMode: ViewMode = isViewMode(rawView) ? rawView : "panes";
  const traceRunId = searchParams.get("run_id");
  const openPaneIds = (searchParams.get("panes") ?? "")
    .split(".")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
  const traceCallId = searchParams.get("call_id");

  const setViewMode = useCallback(
    (mode: ViewMode) => {
      const params = new URLSearchParams(searchParams.toString());
      if (mode === "panes") {
        params.delete("view");
      } else {
        params.set("view", mode);
      }
      // Trace-mode params make no sense outside trace mode.
      // ALSO: clicking "trace" while already on a specific run should
      // return to the trace index (RunPicker) rather than stay on the
      // same run — mirrors the "click tab you're on to go to its home"
      // convention.
      if (mode !== "trace" || viewMode === "trace") {
        params.delete("run_id");
        params.delete("call_id");
      }
      const query = params.toString();
      // push (not replace) so browser back/forward navigate view changes.
      router.push(`${pathname}${query ? `?${query}` : ""}`, {
        scroll: false,
      });
    },
    [searchParams, router, pathname, viewMode],
  );

  const setTraceRun = useCallback(
    (runId: string) => {
      const params = new URLSearchParams(searchParams.toString());
      params.set("view", "trace");
      params.set("run_id", runId);
      params.delete("call_id");
      router.push(`${pathname}?${params.toString()}`, { scroll: false });
    },
    [searchParams, router, pathname],
  );

  const setTraceCall = useCallback(
    (callId: string | null) => {
      const params = new URLSearchParams(searchParams.toString());
      if (callId) {
        params.set("call_id", callId);
      } else {
        params.delete("call_id");
      }
      router.replace(`${pathname}?${params.toString()}`, { scroll: false });
    },
    [searchParams, router, pathname],
  );

  // Chat-triggered navigation. The backend `set_view` tool emits a
  // NavigateDirective inside tool_use_result payloads; ChatPanel parses
  // those and calls onNavigate. We translate the directive into the same
  // URL shape as setViewMode/setTraceRun and preserve params the directive
  // does not touch (e.g. `project`, `q`, `chat`).
  const applyNavigate = useCallback(
    (d: NavigateDirective) => {
      const params = new URLSearchParams(searchParams.toString());
      if (d.view === "panes") {
        params.delete("view");
      } else {
        params.set("view", d.view);
      }
      if (d.view === "trace") {
        if (d.run_id_short || d.run_id) {
          params.set("run_id", d.run_id_short ?? d.run_id!);
        }
        if (d.call_id_short || d.call_id) {
          params.set("call_id", d.call_id_short ?? d.call_id!);
        } else {
          params.delete("call_id");
        }
      } else {
        // Non-trace views: drop trace-specific params so the URL is clean.
        params.delete("run_id");
        params.delete("call_id");
      }
      if (d.panes && d.panes.length > 0) {
        params.set("panes", d.panes.join("."));
      }
      if (d.question_id_short || d.question_id) {
        params.set("q", d.question_id_short ?? d.question_id!);
      }
      const query = params.toString();
      router.push(`${pathname}${query ? `?${query}` : ""}`, { scroll: false });
    },
    [searchParams, router, pathname],
  );

// Register a trace-jump handler so provenance chips anywhere in the tree
  // can call openTrace(runId, callId) and land here with trace mode
  // activated. We re-register whenever the deps change; the ref inside the
  // provider always holds the latest closure.
  useEffect(() => {
    const handler = (runId: string, callId?: string) => {
      const params = new URLSearchParams(searchParams.toString());
      params.set("view", "trace");
      params.set("run_id", runId);
      if (callId) {
        params.set("call_id", callId);
      } else {
        params.delete("call_id");
      }
      router.push(`${pathname}?${params.toString()}`, { scroll: false });
    };
    registerTraceHandler(handler);
    return () => registerTraceHandler(null);
  }, [searchParams, router, pathname, registerTraceHandler]);

const refreshView = useCallback(() => {
    setRefreshKey((k) => k + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetchQuestionView(questionId)
      .then((v) => {
        if (!cancelled) setView(v);
      })
      .catch((e) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey, questionId]);

  // Browser-tab title. TRACE view uses the short run id (the full run name
  // isn't plumbed up from TraceView — short id is a fine fallback that
  // matches the header chip users already see). Other views use the
  // question headline so a user with N tabs open can tell them apart.
  const titleHeadline =
    viewMode === "trace"
      ? traceRunId
        ? `run ${traceRunId.slice(0, 8)}`
        : null
      : (view?.question.headline ?? null);
  useDocumentTitle([viewMode, titleHeadline, project.name]);

  if (error) {
    return (
      <div className="view-error">
        Could not load view: {error}
        <br />
        Is the rumil API running? (./scripts/dev-api.sh)
      </div>
    );
  }

  if (!view) {
    return <div className="view-loading">Loading research...</div>;
  }

  return (
    <ConceptProvider projectId={project.id}>
      <div className="layout-with-chat">
      {showReview ? (
        <div className="pane-container">
          <div className="pane" style={{ minWidth: "500px" }}>
            <SuggestionReview
              projectId={project.id}
              onClose={() => setShowReview(false)}
              onAction={refreshView}
            />
          </div>
        </div>
      ) : (
        <div className="view-content">
          <ViewModeSwitcher
            current={viewMode}
            onChange={setViewMode}
            onBack={onBack}
            label={project.name}
            onRename={onRenameProject}
            extra={
              <>
                {viewMode === "vertical" && (
                  <>
                    <span className="view-switcher-sep" />
                    <button
                      className="view-switcher-btn"
                      onClick={() => verticalRef.current?.expandAll()}
                    >
                      expand
                    </button>
                    <button
                      className="view-switcher-btn"
                      onClick={() => verticalRef.current?.collapseAll()}
                    >
                      collapse
                    </button>
                  </>
                )}
                {viewMode === "article" && (
                  <InlaySelector
                    questionId={questionId}
                    onLoaded={handleInlayLoaded}
                  />
                )}
                <span className="view-switcher-continue-sep" />
                <button
                  type="button"
                  className="view-switcher-continue"
                  onClick={() => setContinueOpen(true)}
                  title="Kick off a new orchestrator run against this question"
                >
                  continue research…
                </button>
              </>
            }
          />
          {viewMode === "panes" && (
            <StackedPanes
              view={view}
              focusNodeId={focusNodeId}
              onFocusHandled={() => setFocusNodeId(null)}
              onOpenSource={setDrawerSource}
            />
          )}
          {viewMode === "article" && inlayState?.selected ? (
            <InlayFrame
              inlay={inlayState.selected}
              view={view}
              project={{ id: project.id, name: project.name }}
            />
          ) : viewMode === "article" ? (
            <ArticleView
              view={view}
              focusNodeId={focusNodeId}
              onFocusHandled={() => setFocusNodeId(null)}
              onOpenSource={setDrawerSource}
            />
          ) : null}
          {viewMode === "vertical" && (
            <VerticalView
              ref={verticalRef}
              view={view}
              focusNodeId={focusNodeId}
              onFocusHandled={() => setFocusNodeId(null)}
            />
          )}
          {viewMode === "sections" && (
            <SectionsView
              view={view}
              onOpenSource={setDrawerSource}
            />
          )}
          {viewMode === "sources" && (
            <SourcesView
              projectId={project.id}
              onOpenDrawer={setDrawerSource}
            />
          )}
          {viewMode === "trace" && (
            <TraceView
              runId={traceRunId}
              projectId={project.id}
              initialCallId={traceCallId}
              onSelectRun={setTraceRun}
              onSelectedCallChange={setTraceCall}
            />
          )}
        </div>
      )}
      <InspectPanel
        shortId={openShortId}
        onClose={closeInspect}
        onOpen={openInspect}
        onPromote={promoteToPane}
      />
      <ChatPanel
        questionId={questionId}
        questionHeadline={view.question.headline}
        isOpen={chatOpen}
        onToggle={toggleChat}
        onMessageSent={refreshView}
        onNodeRef={handleNodeRef}
        onShowReview={() => setShowReview(true)}
        workspace={project.name}
        projectId={project.id}
        openRunId={traceRunId ?? undefined}
        openPageIds={openPaneIds}
        viewMode={viewMode}
        openCallId={traceCallId ?? undefined}
        drawerPageId={openShortId ?? undefined}
        reviewOpen={showReview}
        onNavigate={applyNavigate}
      />
      <SourceDrawer
        source={drawerSource}
        onClose={() => setDrawerSource(null)}
      />
      <SearchPalette
        projectId={project.id}
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        onOpenPage={(page) => {
          // Inspect panel takes a short id — the first 8 chars of the full
          // page id, which is what openInspect/resolve_page_id expects.
          openInspect(page.id.slice(0, 8));
        }}
        onOpenQuestion={(page) => {
          // Navigate to the question view for this project.
          const params = new URLSearchParams();
          params.set("project", project.id);
          params.set("q", page.id);
          router.push(`?${params.toString()}`);
        }}
      />
      {continueOpen && (
        <ContinueResearchModal
          questionId={questionId}
          onClose={() => setContinueOpen(false)}
          onLaunched={(runId) => {
            setContinueOpen(false);
            // Navigate to trace mode so the user watches the run unfold.
            setTraceRun(runId);
          }}
        />
      )}
      </div>
    </ConceptProvider>
  );
}

function AppContent() {
  const searchParams = useSearchParams();
  const questionParam = searchParams.get("q");
  const projectParam = searchParams.get("project");

  const [selectedProject, setSelectedProject] = useState<Project | null>(null);
  const [questions, setQuestions] = useState<Page[] | null>(null);
  const [selectedQuestionId, setSelectedQuestionId] = useState<string | null>(questionParam);
  const [loadingQuestions, setLoadingQuestions] = useState(false);
  // True when the hydration effect ran and couldn't resolve the project —
  // e.g. project not found, or API error. Used to stop showing a loading
  // screen forever on a bad deep link.
  const [hydrationFailed, setHydrationFailed] = useState<boolean>(false);

  // Cold-load hydration from searchParams. Without this, any deep link
  // (`?project=...&q=...&view=...`) would mount with `selectedProject=null`
  // and bounce to the project browser. Deep links must render directly.
  const hydratedProjectRef = useRef<string | null>(null);
  useEffect(() => {
    if (!projectParam) return;
    if (hydratedProjectRef.current === projectParam) return;
    hydratedProjectRef.current = projectParam;
    let cancelled = false;
    (async () => {
      try {
        const projects = await fetchProjects();
        if (cancelled) return;
        // Accept either a project id (the shape we write into the URL) or a
        // project name (the shape `--workspace` uses in API/CLI contexts).
        // This makes links robust to whichever form the caller had.
        const match = projects.find(
          (p) => p.id === projectParam || p.name === projectParam,
        );
        if (!match) {
          setHydrationFailed(true);
          return;
        }
        setSelectedProject(match);
        if (questionParam) {
          setSelectedQuestionId(questionParam);
          return;
        }
        // No question param yet — defer to the existing load-questions
        // effect below to populate the picker.
        setLoadingQuestions(true);
        const qs = await fetchRootQuestions(match.id);
        if (cancelled) return;
        setQuestions(qs);
        if (qs.length === 1) {
          setSelectedQuestionId(qs[0].id);
        }
      } catch {
        /* leave state untouched; falls through to landing */
      } finally {
        if (!cancelled) {
          setLoadingQuestions(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [projectParam, questionParam]);

  const handleSelectProject = useCallback((project: Project) => {
    setSelectedProject(project);
    setSelectedQuestionId(null);
    setLoadingQuestions(true);
    window.history.replaceState(null, "", `?project=${encodeURIComponent(project.id)}`);

    fetchRootQuestions(project.id)
      .then((qs) => {
        setQuestions(qs);
        if (qs.length === 1) {
          setSelectedQuestionId(qs[0].id);
          window.history.replaceState(
            null,
            "",
            `?project=${encodeURIComponent(project.id)}&q=${encodeURIComponent(qs[0].id)}`,
          );
        }
      })
      .catch(() => setQuestions([]))
      .finally(() => setLoadingQuestions(false));
  }, []);

  const handleSelectQuestion = useCallback((question: Page) => {
    setSelectedQuestionId(question.id);
    if (selectedProject) {
      window.history.replaceState(
        null,
        "",
        `?project=${encodeURIComponent(selectedProject.id)}&q=${encodeURIComponent(question.id)}`,
      );
    }
  }, [selectedProject]);

  const handleBackToProjects = useCallback(() => {
    setSelectedProject(null);
    setQuestions(null);
    setSelectedQuestionId(null);
    window.history.replaceState(null, "", "/");
  }, []);

  const handleBackToQuestions = useCallback(() => {
    setSelectedQuestionId(null);
    if (selectedProject) {
      window.history.replaceState(
        null,
        "",
        `?project=${encodeURIComponent(selectedProject.id)}`,
      );
    }
  }, [selectedProject]);

  // Navigate into a newly-created question, mirroring the existing deep-link
  // pattern (`?project=<id>&q=<id>`). Also prepend the new question into the
  // cached list so that if the user later clicks "back" from the view, the
  // picker shows it without a refetch.
  const handleCreateQuestion = useCallback(
    (question: Page) => {
      if (!selectedProject) return;
      setQuestions((prev) => (prev ? [question, ...prev] : [question]));
      setSelectedQuestionId(question.id);
      window.history.replaceState(
        null,
        "",
        `?project=${encodeURIComponent(selectedProject.id)}&q=${encodeURIComponent(question.id)}`,
      );
    },
    [selectedProject],
  );

  // Rename the currently-selected workspace. Throws on 409/422 so the
  // inline-edit UI can surface the server error; on success we swap the
  // project's name in local state so the view-switcher label updates
  // without a round-trip.
  const handleRenameProject = useCallback(
    async (nextName: string) => {
      if (!selectedProject) return;
      const updated = await updateProject(selectedProject.id, {
        name: nextName,
      });
      setSelectedProject((prev) =>
        prev && prev.id === updated.id ? { ...prev, name: updated.name } : prev,
      );
    },
    [selectedProject],
  );

  if (!selectedProject) {
    // If the URL has ?project=X, always show loading until either the
    // hydration effect resolves the project or explicitly marks failure.
    // Using `projectParam` directly (rather than a `hydratingFromUrl` bool
    // that can desync with URL state) makes refresh-with-deep-link robust.
    if (projectParam && !hydrationFailed) {
      return <div className="view-loading">Loading research...</div>;
    }
    return <ProjectBrowser onSelectProject={handleSelectProject} />;
  }

  if (loadingQuestions) {
    return <div className="view-loading">Loading questions...</div>;
  }

  if (selectedQuestionId) {
    return (
      <QuestionViewPage
        project={selectedProject}
        questionId={selectedQuestionId}
        onBack={questions && questions.length > 1 ? handleBackToQuestions : handleBackToProjects}
        onRenameProject={handleRenameProject}
      />
    );
  }

  if (questions && questions.length > 1) {
    return (
      <QuestionPicker
        project={selectedProject}
        questions={questions}
        onSelect={handleSelectQuestion}
        onBack={handleBackToProjects}
        onCreateQuestion={handleCreateQuestion}
      />
    );
  }

  // Empty workspace (or freshly created one): show the ask-a-question form
  // as the primary affordance instead of a dead-end. Same form is used from
  // inside QuestionPicker when a project already has questions.
  return (
    <div className="browser">
      <div className="browser-header">
        <button
          onClick={handleBackToProjects}
          style={{
            background: "none",
            border: "none",
            cursor: "pointer",
            fontFamily: "var(--font-mono-stack)",
            fontSize: "11px",
            color: "var(--fg-dim)",
            padding: "0 0 8px 0",
            letterSpacing: "0.04em",
          }}
        >
          ← projects
        </button>
        <h1 className="browser-title">{selectedProject.name}</h1>
        <p className="browser-subtitle">
          No questions yet.
        </p>
      </div>
      <AskQuestionForm
        projectName={selectedProject.id}
        variant="empty"
        onCreated={handleCreateQuestion}
      />
    </div>
  );
}

export default function Page() {
  return (
    <Suspense
      fallback={<div className="view-loading">Loading...</div>}
    >
      <AnnotationProvider>
        <InspectPanelProvider>
          <AppContent />
        </InspectPanelProvider>
      </AnnotationProvider>
    </Suspense>
  );
}
