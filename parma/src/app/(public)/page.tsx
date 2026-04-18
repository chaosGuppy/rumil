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
import { SourceDrawer } from "@/components/SourceDrawer";
import { ConceptProvider } from "@/components/ConceptContext";
import {
  InspectPanelProvider,
  useInspectPanel,
} from "@/components/InspectPanelContext";
import {
  fetchProjects,
  fetchProjectsSummary,
  fetchRootQuestions,
  fetchQuestionView,
} from "@/lib/api";
import type { QuestionView, Page, Project, ProjectSummary } from "@/lib/types";

const TEST_PROJECT_PATTERN = /^(test|scratch|smoke|tmp|scratchpad|skyblue-scratch|test-scratch)([-_].*)?$/i;

function isTestProject(name: string): boolean {
  return TEST_PROJECT_PATTERN.test(name);
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
const SORT_STORAGE_KEY = "parma:projectSort";

function loadShowTest(): boolean {
  if (typeof window === "undefined") return false;
  return window.localStorage.getItem(SHOW_TEST_STORAGE_KEY) === "1";
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

const VIEW_MODES = ["panes", "article", "vertical", "sources"] as const;
type ViewMode = (typeof VIEW_MODES)[number];

function isViewMode(v: string): v is ViewMode {
  return (VIEW_MODES as readonly string[]).includes(v);
}

function ViewModeSwitcher({
  current,
  onChange,
  extra,
  onBack,
  label,
}: {
  current: ViewMode;
  onChange: (mode: ViewMode) => void;
  extra?: React.ReactNode;
  onBack?: () => void;
  label?: string;
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
                {label}
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

function ProjectBrowser({
  onSelectProject,
}: {
  onSelectProject: (project: Project) => void;
}) {
  const [rows, setRows] = useState<ProjectSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showTest, setShowTest] = useState(false);
  const [sort, setSort] = useState<SortMode>("newest");

  // Hydrate UI preferences from localStorage. Deferred to an effect so the
  // first render matches the server and we don't flash-unhydrate.
  useEffect(() => {
    setShowTest(loadShowTest());
    setSort(loadSort());
  }, []);

  useEffect(() => {
    fetchProjectsSummary()
      .then(setRows)
      .catch((e) => setError(e?.message ?? "failed"));
  }, []);

  const persistShowTest = useCallback((next: boolean) => {
    setShowTest(next);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(SHOW_TEST_STORAGE_KEY, next ? "1" : "0");
    }
  }, []);

  const persistSort = useCallback((next: SortMode) => {
    setSort(next);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(SORT_STORAGE_KEY, next);
    }
  }, []);

  const filtered = useMemo(() => {
    if (!rows) return null;
    const live = showTest ? rows : rows.filter((r) => !isTestProject(r.name));
    return sortProjects(live, sort);
  }, [rows, showTest, sort]);

  const hiddenTestCount = useMemo(() => {
    if (!rows) return 0;
    return showTest ? 0 : rows.filter((r) => isTestProject(r.name)).length;
  }, [rows, showTest]);

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
          <h1 className="landing-title">Research</h1>
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
        </div>
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
            return (
              <button
                key={p.id}
                className={`landing-card ${empty ? "is-empty" : ""}`}
                onClick={() =>
                  onSelectProject({
                    id: p.id,
                    name: p.name,
                    created_at: p.created_at,
                    hidden: p.hidden,
                  })
                }
              >
                <div className="landing-card-top">
                  <div className="landing-card-name">{p.name}</div>
                  {empty && (
                    <span className="landing-card-empty-badge">empty</span>
                  )}
                </div>

                <dl className="landing-card-stats">
                  <div className="landing-stat">
                    <dt>questions</dt>
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
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function QuestionPicker({
  project,
  questions,
  onSelect,
  onBack,
}: {
  project: Project;
  questions: Page[];
  onSelect: (question: Page) => void;
  onBack: () => void;
}) {
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
        <h1 className="browser-title">{project.name}</h1>
        <p className="browser-subtitle">
          {questions.length} root question{questions.length !== 1 ? "s" : ""}
        </p>
      </div>

      <div className="browser-list">
        {questions.map((q) => (
          <button
            key={q.id}
            className="browser-card"
            onClick={() => onSelect(q)}
          >
            <div className="browser-card-name">{q.headline}</div>
            <div className="browser-card-stats">
              {q.id.slice(0, 8)}
              {" · "}
              {new Date(q.created_at).toLocaleDateString("en-US", {
                year: "numeric",
                month: "short",
                day: "numeric",
              })}
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

function QuestionViewPage({
  project,
  questionId,
  onBack,
}: {
  project: Project;
  questionId: string;
  onBack: () => void;
}) {
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const { openInspect } = useInspectPanel();

  const verticalRef = useRef<VerticalViewHandle>(null);
  const [chatOpen, setChatOpen] = useState(false);
  const toggleChat = useCallback(() => setChatOpen((v) => !v), []);
  const [view, setView] = useState<QuestionView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [focusNodeId, setFocusNodeId] = useState<string | null>(null);
  const [showReview, setShowReview] = useState(false);
  const [drawerSource, setDrawerSource] = useState<Page | null>(null);

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

  const setViewMode = useCallback(
    (mode: ViewMode) => {
      const params = new URLSearchParams(searchParams.toString());
      if (mode === "panes") {
        params.delete("view");
      } else {
        params.set("view", mode);
      }
      const query = params.toString();
      router.replace(`${pathname}${query ? `?${query}` : ""}`, {
        scroll: false,
      });
    },
    [searchParams, router, pathname],
  );

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
            extra={viewMode === "vertical" ? (
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
            ) : undefined}
          />
          {viewMode === "panes" && (
            <StackedPanes
              view={view}
              focusNodeId={focusNodeId}
              onFocusHandled={() => setFocusNodeId(null)}
              onOpenSource={setDrawerSource}
            />
          )}
          {viewMode === "article" && (
            <ArticleView
              view={view}
              focusNodeId={focusNodeId}
              onFocusHandled={() => setFocusNodeId(null)}
              onOpenSource={setDrawerSource}
            />
          )}
          {viewMode === "vertical" && (
            <VerticalView
              ref={verticalRef}
              view={view}
              focusNodeId={focusNodeId}
              onFocusHandled={() => setFocusNodeId(null)}
            />
          )}
          {viewMode === "sources" && (
            <SourcesView
              projectId={project.id}
              onOpenDrawer={setDrawerSource}
            />
          )}
        </div>
      )}
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
      />
      <SourceDrawer
        source={drawerSource}
        onClose={() => setDrawerSource(null)}
      />
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
  // Distinct from loadingQuestions: true while we're resolving the
  // ?project=<id-or-name> URL param on cold load, so we don't flash the
  // landing page before the hydrate effect settles.
  const [hydratingFromUrl, setHydratingFromUrl] = useState<boolean>(
    Boolean(projectParam),
  );
  // True when the hydration effect ran and couldn't resolve the project —
  // e.g. project not found, or API error. Used to stop showing a loading
  // screen forever on a bad deep link.
  const [hydrationFailed, setHydrationFailed] = useState<boolean>(false);

  // Cold-load hydration from searchParams. Without this, any deep link
  // (`?project=...&q=...&view=...`) would mount with `selectedProject=null`
  // and bounce to the project browser. Deep links must render directly.
  const hydratedProjectRef = useRef<string | null>(null);
  useEffect(() => {
    if (!projectParam) {
      setHydratingFromUrl(false);
      return;
    }
    if (hydratedProjectRef.current === projectParam) return;
    hydratedProjectRef.current = projectParam;
    let cancelled = false;
    setHydratingFromUrl(true);
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
          setHydratingFromUrl(false);
          return;
        }
        setSelectedProject(match);
        if (questionParam) {
          setSelectedQuestionId(questionParam);
          setHydratingFromUrl(false);
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
          setHydratingFromUrl(false);
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
      />
    );
  }

  return (
    <div className="view-error">
      No questions found in this project.
      <br />
      <button
        onClick={handleBackToProjects}
        style={{
          marginTop: "12px",
          background: "none",
          border: "1px solid var(--border)",
          padding: "8px 16px",
          cursor: "pointer",
          fontFamily: "var(--font-mono-stack)",
          fontSize: "12px",
          color: "var(--fg-muted)",
        }}
      >
        ← back to projects
      </button>
    </div>
  );
}

export default function Page() {
  return (
    <Suspense
      fallback={<div className="view-loading">Loading...</div>}
    >
      <InspectPanelProvider>
        <AppContent />
      </InspectPanelProvider>
    </Suspense>
  );
}
