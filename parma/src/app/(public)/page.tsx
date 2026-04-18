"use client";

import { useState, useCallback, useEffect, useRef } from "react";
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
import { fetchProjects, fetchRootQuestions, fetchQuestionView } from "@/lib/api";
import type { QuestionView, Page, Project } from "@/lib/types";

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
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchProjects()
      .then(setProjects)
      .catch(() => setProjects([]))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="browser-loading">Loading projects...</div>
    );
  }

  return (
    <div className="browser">
      <div className="browser-header">
        <h1 className="browser-title">Research</h1>
        <p className="browser-subtitle">
          Research projects. Pick one to explore.
        </p>
      </div>

      {projects.length > 0 && (
        <div className="browser-list">
          {projects.map((project) => (
            <button
              key={project.id}
              className="browser-card"
              onClick={() => onSelectProject(project)}
            >
              <div className="browser-card-name">{project.name}</div>
              <div className="browser-card-stats">
                {new Date(project.created_at).toLocaleDateString("en-US", {
                  year: "numeric",
                  month: "short",
                  day: "numeric",
                })}
              </div>
            </button>
          ))}
        </div>
      )}

      {projects.length === 0 && (
        <div style={{ padding: "20px 0", color: "var(--fg-muted)", fontSize: "14px" }}>
          No projects found. Start the rumil API and create a workspace.
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

  const verticalRef = useRef<VerticalViewHandle>(null);
  const [chatOpen, setChatOpen] = useState(false);
  const toggleChat = useCallback(() => setChatOpen((v) => !v), []);
  const [view, setView] = useState<QuestionView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [focusNodeId, setFocusNodeId] = useState<string | null>(null);
  const [showReview, setShowReview] = useState(false);
  const [drawerSource, setDrawerSource] = useState<Page | null>(null);

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
        onNodeRef={setFocusNodeId}
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

  const [selectedProject, setSelectedProject] = useState<Project | null>(null);
  const [questions, setQuestions] = useState<Page[] | null>(null);
  const [selectedQuestionId, setSelectedQuestionId] = useState<string | null>(questionParam);
  const [loadingQuestions, setLoadingQuestions] = useState(false);

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
      <AppContent />
    </Suspense>
  );
}
