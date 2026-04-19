"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Call, PageDetailOut, Project } from "@/api";
import { CLIENT_API_BASE as API_BASE } from "@/api-config";
import { WorkspaceIndicator } from "@/components/workspace-indicator";
import { useDocumentTitle } from "@/lib/use-document-title";
import { truncateHeadline } from "@/lib/page-titles";

type EvalType = "default" | "grounding" | "feedback";

const EVAL_TYPES: EvalType[] = ["default", "grounding", "feedback"];

const EVAL_TYPE_COPY: Record<EvalType, { title: string; sub: string; glyph: string }> = {
  default: {
    title: "Default",
    sub: "Balanced dimension-by-dimension critique.",
    glyph: "\u25C6",
  },
  grounding: {
    title: "Grounding",
    sub: "Stress-tests citations, provenance, and factual anchors.",
    glyph: "\u25B2",
  },
  feedback: {
    title: "Feedback",
    sub: "Reads the run as if you were the next researcher picking it up.",
    glyph: "\u25CF",
  },
};

function formatDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatCost(cost: number | null | undefined): string {
  if (cost == null) return "—";
  if (cost < 0.01) return `$${cost.toFixed(4)}`;
  return `$${cost.toFixed(3)}`;
}

function getEvalType(call: Call): EvalType {
  const params = (call.call_params ?? {}) as Record<string, unknown>;
  const raw = typeof params["eval_type"] === "string" ? params["eval_type"] : "default";
  if (raw === "grounding" || raw === "feedback") return raw;
  return "default";
}

function getEvaluationText(call: Call): string | null {
  const rj = call.review_json as Record<string, unknown> | null | undefined;
  if (!rj) return null;
  const ev = rj["evaluation"];
  return typeof ev === "string" && ev.trim().length > 0 ? ev : null;
}

function preview(text: string, chars = 180): string {
  const stripped = text
    .replace(/^#+\s*/gm, "")
    .replace(/[*_`>]/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (stripped.length <= chars) return stripped;
  return stripped.slice(0, chars).trimEnd() + "\u2026";
}

type LoadState =
  | { kind: "loading" }
  | { kind: "not-found" }
  | { kind: "not-question"; pageType: string }
  | { kind: "error"; message: string }
  | {
      kind: "ready";
      projectId: string;
      headline: string;
      calls: Call[];
    };

export default function QuestionEvaluationsPage() {
  const params = useParams<{ pageId: string }>();
  const search = useSearchParams();
  const router = useRouter();
  const pageId = params.pageId;
  const stagedRunId = search.get("staged_run_id") ?? undefined;

  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [projectName, setProjectName] = useState<string>();
  const [pickerOpen, setPickerOpen] = useState(false);
  const [firing, setFiring] = useState(false);
  const [fireError, setFireError] = useState<string | null>(null);

  const titleHeadline = state.kind === "ready" ? state.headline : null;
  const titleLabel = titleHeadline
    ? `question "${truncateHeadline(titleHeadline, 42)}" \u00B7 evaluations`
    : null;
  const wsSuffix = projectName ? ` \u2014 ${projectName}` : "";
  useDocumentTitle(titleLabel ? `${titleLabel}${wsSuffix}` : null);

  const loadCalls = useCallback(
    async (projectId: string): Promise<Call[]> => {
      const res = await fetch(
        `${API_BASE}/api/projects/${projectId}/calls?question_id=${pageId}`,
        { cache: "no-store" },
      );
      if (!res.ok) throw new Error(`calls ${res.status}`);
      const all = (await res.json()) as Call[];
      return all
        .filter((c) => c.call_type === "evaluate")
        .sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
    },
    [pageId],
  );

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const detailRes = await fetch(
          `${API_BASE}/api/pages/${pageId}/detail`,
          { cache: "no-store" },
        );
        if (detailRes.status === 404) {
          if (!cancelled) setState({ kind: "not-found" });
          return;
        }
        if (!detailRes.ok) throw new Error(`detail ${detailRes.status}`);
        const detail = (await detailRes.json()) as PageDetailOut;
        if (cancelled) return;

        if (detail.page.page_type !== "question") {
          setState({ kind: "not-question", pageType: detail.page.page_type });
          return;
        }
        const projectId = detail.page.project_id;
        const calls = await loadCalls(projectId);
        if (cancelled) return;
        setState({
          kind: "ready",
          projectId,
          headline: detail.page.headline,
          calls,
        });
      } catch (err) {
        if (!cancelled) setState({ kind: "error", message: String(err) });
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [pageId, loadCalls]);

  useEffect(() => {
    if (state.kind !== "ready") return;
    fetch(`${API_BASE}/api/projects/${state.projectId}`, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d: Project | null) => {
        if (d) setProjectName(d.name);
      });
  }, [state]);

  useEffect(() => {
    if (!pickerOpen) return;
    function onDocClick(e: MouseEvent) {
      const t = e.target as HTMLElement;
      if (!t.closest(".eval-picker") && !t.closest(".eval-fire-btn")) {
        setPickerOpen(false);
      }
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [pickerOpen]);

  async function fireEvaluate(evalType: EvalType) {
    setPickerOpen(false);
    setFireError(null);
    setFiring(true);
    try {
      const res = await fetch(
        `${API_BASE}/api/questions/${pageId}/evaluate`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ eval_type: evalType }),
        },
      );
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `HTTP ${res.status}`);
      }
      const data = (await res.json()) as { run_id: string };
      router.push(`/traces/${data.run_id}`);
    } catch (err) {
      setFireError(String(err));
      setFiring(false);
    }
  }

  const evalCounts = useMemo(() => {
    if (state.kind !== "ready") return null;
    const counts: Record<EvalType, number> = { default: 0, grounding: 0, feedback: 0 };
    for (const c of state.calls) counts[getEvalType(c)]++;
    return counts;
  }, [state]);

  return (
    <main className="evals-page">
      <style>{STYLES}</style>

      {state.kind === "ready" && (
        <WorkspaceIndicator projectId={state.projectId} projectName={projectName} />
      )}

      <nav className="evals-nav">
        <Link href={`/pages/${pageId}${stagedRunId ? `?staged_run_id=${stagedRunId}` : ""}`}>
          <span className="evals-nav-arrow">{"\u2190"}</span> Question
        </Link>
        <Link href={`/pages/${pageId}/stats${stagedRunId ? `?staged_run_id=${stagedRunId}` : ""}`}>
          Stats
        </Link>
      </nav>

      <header className="evals-header">
        <div className="evals-header-line">
          <div className="evals-head-mark">EVAL</div>
          <h1 className="evals-title">Evaluations</h1>
          <div className="evals-head-count">
            {state.kind === "ready"
              ? `${state.calls.length} run${state.calls.length === 1 ? "" : "s"}`
              : ""}
          </div>
        </div>
        {state.kind === "ready" && (
          <div className="evals-question">
            <span className="evals-q-label">on</span>
            <span className="evals-q-headline">{state.headline}</span>
          </div>
        )}
      </header>

      <section className="evals-actions">
        <div className="evals-actions-left">
          <div className="eval-fire-wrap">
            <button
              className="eval-fire-btn"
              onClick={() => setPickerOpen((v) => !v)}
              disabled={firing || state.kind !== "ready"}
              data-open={pickerOpen}
            >
              <span className="eval-fire-reticle" />
              <span className="eval-fire-label">
                {firing ? "dispatching\u2026" : "Evaluate"}
              </span>
              <span className="eval-fire-caret">{pickerOpen ? "\u25B2" : "\u25BC"}</span>
            </button>
            {pickerOpen && (
              <div className="eval-picker" role="menu">
                <div className="eval-picker-head">choose an agent</div>
                {EVAL_TYPES.map((t) => (
                  <button
                    key={t}
                    className="eval-picker-item"
                    data-type={t}
                    onClick={() => fireEvaluate(t)}
                    role="menuitem"
                  >
                    <span className="eval-picker-glyph">
                      {EVAL_TYPE_COPY[t].glyph}
                    </span>
                    <div className="eval-picker-body">
                      <div className="eval-picker-title">{EVAL_TYPE_COPY[t].title}</div>
                      <div className="eval-picker-sub">{EVAL_TYPE_COPY[t].sub}</div>
                    </div>
                    {evalCounts && (
                      <span className="eval-picker-count">
                        {evalCounts[t]}
                      </span>
                    )}
                  </button>
                ))}
              </div>
            )}
          </div>
          {fireError && <div className="eval-fire-error">{fireError}</div>}
        </div>
        {evalCounts && (
          <div className="evals-legend">
            {EVAL_TYPES.map((t) => (
              <span key={t} className="evals-legend-item" data-type={t}>
                <span className="evals-legend-swatch" />
                <span className="evals-legend-name">{t}</span>
                <span className="evals-legend-n">{evalCounts[t]}</span>
              </span>
            ))}
          </div>
        )}
      </section>

      {state.kind === "loading" && (
        <div className="evals-placeholder evals-loading">Loading evaluations{"\u2026"}</div>
      )}
      {state.kind === "not-found" && (
        <div className="evals-placeholder">Page not found.</div>
      )}
      {state.kind === "not-question" && (
        <div className="evals-placeholder">
          Evaluations are only available for question pages.
          <div className="evals-placeholder-hint">
            this page is a <em>{state.pageType}</em>
          </div>
        </div>
      )}
      {state.kind === "error" && (
        <div className="evals-placeholder evals-error">
          Failed to load: {state.message}
        </div>
      )}
      {state.kind === "ready" && state.calls.length === 0 && (
        <div className="evals-empty">
          <div className="evals-empty-rule" />
          <div className="evals-empty-msg">no evaluations yet</div>
          <div className="evals-empty-hint">
            dispatch one with the button above
          </div>
          <div className="evals-empty-rule" />
        </div>
      )}
      {state.kind === "ready" && state.calls.length > 0 && (
        <div className="evals-list">
          {state.calls.map((call, i) => (
            <EvalCard
              key={call.id}
              call={call}
              index={state.calls.length - i}
              onAfterAction={() => {
                // re-fetch after a follow-up action to capture new runs
                loadCalls(state.projectId)
                  .then((calls) =>
                    setState((prev) =>
                      prev.kind === "ready" ? { ...prev, calls } : prev,
                    ),
                  )
                  .catch(() => {});
              }}
            />
          ))}
        </div>
      )}
    </main>
  );
}

function EvalCard({
  call,
  index,
  onAfterAction,
}: {
  call: Call;
  index: number;
  onAfterAction: () => void;
}) {
  const router = useRouter();
  const evalType = getEvalType(call);
  const initialText = getEvaluationText(call);

  const [open, setOpen] = useState(false);
  const [fullCall, setFullCall] = useState<Call | null>(
    initialText ? call : null,
  );
  const [loading, setLoading] = useState(false);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [followupBusy, setFollowupBusy] = useState<null | "ground" | "feedback">(
    null,
  );
  const [followupErr, setFollowupErr] = useState<string | null>(null);

  const evaluation = fullCall ? getEvaluationText(fullCall) : initialText;
  const previewText = initialText ? preview(initialText) : null;

  async function toggle() {
    const next = !open;
    setOpen(next);
    if (next && !fullCall) {
      setLoading(true);
      setLoadErr(null);
      try {
        const res = await fetch(`${API_BASE}/api/calls/${call.id}`, {
          cache: "no-store",
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = (await res.json()) as Call;
        setFullCall(data);
      } catch (err) {
        setLoadErr(String(err));
      } finally {
        setLoading(false);
      }
    }
  }

  async function runFollowup(kind: "ground" | "feedback") {
    setFollowupBusy(kind);
    setFollowupErr(null);
    try {
      const path = kind === "ground" ? "ground" : "feedback";
      const res = await fetch(`${API_BASE}/api/calls/${call.id}/${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `HTTP ${res.status}`);
      }
      const data = (await res.json()) as { run_id: string };
      onAfterAction();
      router.push(`/traces/${data.run_id}`);
    } catch (err) {
      setFollowupErr(String(err));
      setFollowupBusy(null);
    }
  }

  const isComplete = call.status === "complete" && evaluation != null;

  return (
    <article
      className="eval-card"
      data-type={evalType}
      data-open={open}
      data-status={call.status}
      style={{ animationDelay: `${Math.min(index * 18, 240)}ms` }}
    >
      <button className="eval-card-head" onClick={toggle}>
        <span className="eval-card-index">{String(index).padStart(2, "0")}</span>
        <span className="eval-card-rail" />
        <span className="eval-card-type">
          <span className="eval-card-glyph">{EVAL_TYPE_COPY[evalType].glyph}</span>
          <span className="eval-card-type-name">{evalType}</span>
        </span>
        <span className="eval-card-main">
          <span className="eval-card-preview">
            {previewText ||
              (call.status === "complete"
                ? "no evaluation text"
                : call.status === "failed"
                  ? "evaluation failed"
                  : call.status === "running"
                    ? "evaluation running\u2026"
                    : "pending")}
          </span>
          <span className="eval-card-meta">
            <span className="eval-card-status" data-status={call.status}>
              {call.status}
            </span>
            <span className="eval-card-sep" />
            <span>{formatDate(call.created_at)}</span>
            <span className="eval-card-sep" />
            <span>{formatCost(call.cost_usd)}</span>
            <span className="eval-card-sep" />
            <span className="eval-card-id">{call.id.slice(0, 8)}</span>
          </span>
        </span>
        <span className="eval-card-toggle">{open ? "\u2212" : "+"}</span>
      </button>

      {open && (
        <div className="eval-card-body">
          <div className="eval-card-body-tabs">
            <Link
              href={`/traces/${call.id}`}
              className="eval-card-trace-link"
            >
              trace {call.id.slice(0, 8)} {"\u2197"}
            </Link>
          </div>

          {loading && (
            <div className="eval-card-loading">fetching report{"\u2026"}</div>
          )}
          {loadErr && (
            <div className="eval-card-loading eval-card-error">
              failed to load: {loadErr}
            </div>
          )}
          {!loading && !loadErr && evaluation && (
            <div className="eval-card-markdown">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {evaluation}
              </ReactMarkdown>
            </div>
          )}
          {!loading && !loadErr && !evaluation && (
            <div className="eval-card-loading">
              no evaluation output — see trace for details.
            </div>
          )}

          {isComplete && (
            <div className="eval-card-followups">
              <div className="eval-card-followups-label">follow-up pipelines</div>
              <div className="eval-card-followup-row">
                <button
                  className="eval-card-followup-btn"
                  data-kind="ground"
                  disabled={followupBusy !== null}
                  onClick={() => runFollowup("ground")}
                >
                  <span className="eval-card-followup-glyph">{"\u25B3"}</span>
                  <span>
                    {followupBusy === "ground"
                      ? "dispatching\u2026"
                      : "Run grounding pipeline"}
                  </span>
                </button>
                <button
                  className="eval-card-followup-btn"
                  data-kind="feedback"
                  disabled={followupBusy !== null}
                  onClick={() => runFollowup("feedback")}
                >
                  <span className="eval-card-followup-glyph">{"\u25CB"}</span>
                  <span>
                    {followupBusy === "feedback"
                      ? "dispatching\u2026"
                      : "Run feedback pipeline"}
                  </span>
                </button>
              </div>
              {followupErr && (
                <div className="eval-card-followup-err">{followupErr}</div>
              )}
            </div>
          )}
        </div>
      )}
    </article>
  );
}

const STYLES = `
  .evals-page {
    max-width: 60rem;
    margin: 0 auto;
    padding: 2rem 2rem 4rem;
    font-family: var(--font-geist-sans), system-ui, sans-serif;
    color: var(--color-foreground);
  }

  .evals-nav {
    display: flex;
    gap: 0.4rem;
    margin-bottom: 1.75rem;
  }
  .evals-nav a {
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.68rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--color-muted);
    text-decoration: none;
    border: 1px solid var(--color-border);
    padding: 0.35rem 0.7rem;
    transition: color 0.12s, border-color 0.12s, background 0.12s;
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
  }
  .evals-nav a:hover {
    color: var(--color-foreground);
    border-color: var(--color-accent);
    background: var(--color-surface);
  }
  .evals-nav-arrow {
    display: inline-block;
    font-size: 0.8em;
    opacity: 0.6;
  }

  .evals-header {
    border-top: 2px solid var(--color-foreground);
    padding-top: 1rem;
    margin-bottom: 1.75rem;
    position: relative;
  }
  .evals-header::before {
    content: "";
    position: absolute;
    top: -2px;
    left: 0;
    width: 4rem;
    height: 6px;
    background: var(--color-foreground);
  }
  .evals-header-line {
    display: flex;
    align-items: baseline;
    gap: 1rem;
    flex-wrap: wrap;
  }
  .evals-head-mark {
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.6rem;
    letter-spacing: 0.28em;
    font-weight: 700;
    color: var(--color-muted);
    padding: 0.1rem 0.3rem;
    border: 1px solid var(--color-foreground);
    color: var(--color-foreground);
    background: var(--color-foreground);
    color: var(--color-background);
  }
  .evals-title {
    font-size: 2.1rem;
    font-weight: 700;
    letter-spacing: -0.035em;
    margin: 0;
    line-height: 1;
  }
  .evals-head-count {
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.7rem;
    color: var(--color-muted);
    letter-spacing: 0.06em;
    margin-left: auto;
  }

  .evals-question {
    margin-top: 0.75rem;
    display: flex;
    gap: 0.5rem;
    align-items: baseline;
    max-width: 48rem;
  }
  .evals-q-label {
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.65rem;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--color-muted);
    flex-shrink: 0;
  }
  .evals-q-headline {
    font-size: 0.92rem;
    font-weight: 500;
    line-height: 1.45;
    color: var(--color-foreground);
    opacity: 0.85;
    font-style: italic;
  }

  .evals-actions {
    display: flex;
    align-items: center;
    gap: 1rem;
    margin-bottom: 2rem;
    flex-wrap: wrap;
    border-bottom: 1px dashed var(--color-border);
    padding-bottom: 1rem;
  }
  .evals-actions-left {
    flex: 1;
    min-width: 12rem;
  }
  .eval-fire-wrap {
    position: relative;
    display: inline-block;
  }
  .eval-fire-btn {
    display: inline-flex;
    align-items: center;
    gap: 0.55rem;
    background: var(--color-foreground);
    color: var(--color-background);
    border: none;
    padding: 0.55rem 0.95rem 0.55rem 0.75rem;
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.72rem;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    cursor: pointer;
    transition: transform 0.08s ease, opacity 0.12s ease;
    box-shadow: 3px 3px 0 var(--eval-default);
  }
  .eval-fire-btn:hover:not(:disabled) {
    transform: translate(-1px, -1px);
    box-shadow: 4px 4px 0 var(--eval-default);
  }
  .eval-fire-btn:active:not(:disabled) {
    transform: translate(1px, 1px);
    box-shadow: 1px 1px 0 var(--eval-default);
  }
  .eval-fire-btn:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .eval-fire-btn[data-open="true"] {
    box-shadow: 1px 1px 0 var(--eval-default);
    transform: translate(1px, 1px);
  }
  .eval-fire-reticle {
    width: 10px;
    height: 10px;
    border: 1.5px solid var(--color-background);
    border-radius: 50%;
    position: relative;
  }
  .eval-fire-reticle::before,
  .eval-fire-reticle::after {
    content: "";
    position: absolute;
    background: var(--color-background);
  }
  .eval-fire-reticle::before {
    left: -3px;
    right: -3px;
    top: 50%;
    height: 1.5px;
    transform: translateY(-50%);
  }
  .eval-fire-reticle::after {
    top: -3px;
    bottom: -3px;
    left: 50%;
    width: 1.5px;
    transform: translateX(-50%);
  }
  .eval-fire-caret {
    font-size: 0.55rem;
    opacity: 0.7;
  }

  .eval-picker {
    position: absolute;
    top: calc(100% + 6px);
    left: 0;
    z-index: 20;
    min-width: 22rem;
    background: var(--color-background);
    border: 1px solid var(--color-foreground);
    box-shadow: 4px 4px 0 var(--color-border);
    animation: pickerDrop 0.12s ease both;
  }
  @keyframes pickerDrop {
    from { opacity: 0; transform: translateY(-4px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .eval-picker-head {
    padding: 0.4rem 0.75rem;
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.6rem;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: var(--color-muted);
    border-bottom: 1px dashed var(--color-border);
  }
  .eval-picker-item {
    display: flex;
    align-items: flex-start;
    gap: 0.7rem;
    width: 100%;
    padding: 0.7rem 0.8rem;
    background: transparent;
    border: none;
    border-bottom: 1px solid var(--color-border);
    text-align: left;
    cursor: pointer;
    transition: background 0.1s ease;
    color: var(--color-foreground);
  }
  .eval-picker-item:last-child {
    border-bottom: none;
  }
  .eval-picker-item:hover {
    background: var(--color-surface);
  }
  .eval-picker-glyph {
    flex-shrink: 0;
    width: 1.2rem;
    font-size: 0.85rem;
    line-height: 1.2rem;
    text-align: center;
    color: var(--eval-color);
    margin-top: 0.1rem;
  }
  .eval-picker-body {
    flex: 1;
    min-width: 0;
  }
  .eval-picker-title {
    font-family: var(--font-geist-sans), system-ui, sans-serif;
    font-size: 0.82rem;
    font-weight: 600;
    letter-spacing: 0.01em;
    margin-bottom: 0.15rem;
  }
  .eval-picker-sub {
    font-size: 0.72rem;
    line-height: 1.4;
    color: var(--color-muted);
  }
  .eval-picker-count {
    flex-shrink: 0;
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.65rem;
    color: var(--color-muted);
    background: var(--color-surface);
    padding: 0.1rem 0.4rem;
    border: 1px solid var(--color-border);
    align-self: flex-start;
  }
  .eval-picker-item[data-type="default"] { --eval-color: var(--eval-default); }
  .eval-picker-item[data-type="grounding"] { --eval-color: var(--eval-grounding); }
  .eval-picker-item[data-type="feedback"] { --eval-color: var(--eval-feedback); }

  .eval-fire-error {
    margin-top: 0.5rem;
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.7rem;
    color: var(--dir-opposes);
  }

  .evals-legend {
    display: flex;
    gap: 0.9rem;
    flex-wrap: wrap;
  }
  .evals-legend-item {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.65rem;
    letter-spacing: 0.06em;
    color: var(--color-muted);
    text-transform: uppercase;
  }
  .evals-legend-swatch {
    width: 10px;
    height: 10px;
    background: var(--eval-color);
    display: inline-block;
  }
  .evals-legend-item[data-type="default"] { --eval-color: var(--eval-default); }
  .evals-legend-item[data-type="grounding"] { --eval-color: var(--eval-grounding); }
  .evals-legend-item[data-type="feedback"] { --eval-color: var(--eval-feedback); }
  .evals-legend-n {
    color: var(--color-foreground);
    font-weight: 600;
  }

  .evals-placeholder {
    padding: 4rem 1rem;
    text-align: center;
    color: var(--color-muted);
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.8rem;
    border: 1px dashed var(--color-border);
  }
  .evals-placeholder-hint {
    font-size: 0.7rem;
    margin-top: 0.5rem;
    opacity: 0.7;
  }
  .evals-loading {
    animation: evalsPulse 1.6s ease infinite;
  }
  @keyframes evalsPulse {
    0%, 100% { opacity: 0.6; }
    50% { opacity: 1; }
  }
  .evals-error {
    color: var(--dir-opposes);
  }

  .evals-empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.5rem;
    padding: 3rem 1rem;
    color: var(--color-muted);
    font-family: var(--font-geist-mono), monospace;
  }
  .evals-empty-rule {
    width: 6rem;
    height: 1px;
    background: var(--color-border);
  }
  .evals-empty-msg {
    font-size: 0.85rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }
  .evals-empty-hint {
    font-size: 0.7rem;
    opacity: 0.7;
  }

  .evals-list {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }

  .eval-card {
    background: var(--color-background);
    border: 1px solid var(--color-border);
    border-left: 3px solid var(--eval-color);
    transition: border-color 0.15s ease, background 0.15s ease;
    animation: evalCardIn 0.25s ease both;
  }
  @keyframes evalCardIn {
    from { opacity: 0; transform: translateY(3px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .eval-card[data-type="default"] { --eval-color: var(--eval-default); }
  .eval-card[data-type="grounding"] { --eval-color: var(--eval-grounding); }
  .eval-card[data-type="feedback"] { --eval-color: var(--eval-feedback); }
  .eval-card[data-open="true"] {
    border-color: var(--eval-color);
    background: var(--color-surface);
  }
  .eval-card[data-status="failed"] {
    opacity: 0.75;
  }
  .eval-card[data-status="running"] .eval-card-index {
    animation: evalsPulse 1.4s ease infinite;
  }

  .eval-card-head {
    display: grid;
    grid-template-columns: auto auto auto 1fr auto;
    align-items: stretch;
    gap: 0.6rem;
    width: 100%;
    padding: 0.65rem 0.8rem;
    background: transparent;
    border: none;
    text-align: left;
    cursor: pointer;
    font: inherit;
    color: inherit;
    transition: background 0.12s ease;
  }
  .eval-card-head:hover {
    background: var(--color-surface);
  }

  .eval-card-index {
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.7rem;
    font-weight: 700;
    color: var(--color-muted);
    letter-spacing: 0.04em;
    align-self: center;
    min-width: 1.6rem;
  }
  .eval-card[data-open="true"] .eval-card-index {
    color: var(--eval-color);
  }
  .eval-card-rail {
    width: 1px;
    background: var(--color-border);
    align-self: stretch;
  }
  .eval-card-type {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.7rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--eval-color);
    font-weight: 600;
    align-self: center;
    min-width: 5.5rem;
  }
  .eval-card-glyph {
    font-size: 0.8rem;
  }
  .eval-card-main {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    min-width: 0;
  }
  .eval-card-preview {
    font-size: 0.82rem;
    line-height: 1.4;
    color: var(--color-foreground);
    opacity: 0.88;
    overflow: hidden;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }
  .eval-card-meta {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.65rem;
    color: var(--color-muted);
    flex-wrap: wrap;
  }
  .eval-card-sep {
    width: 2px;
    height: 2px;
    border-radius: 50%;
    background: var(--color-muted);
    opacity: 0.5;
  }
  .eval-card-status {
    text-transform: uppercase;
    letter-spacing: 0.1em;
    font-weight: 600;
  }
  .eval-card-status[data-status="complete"] { color: var(--dir-supports); }
  .eval-card-status[data-status="failed"] { color: var(--dir-opposes); }
  .eval-card-status[data-status="running"] { color: var(--eval-feedback); }
  .eval-card-status[data-status="pending"] { color: var(--color-muted); }
  .eval-card-id {
    opacity: 0.7;
  }
  .eval-card-toggle {
    font-family: var(--font-geist-mono), monospace;
    font-size: 1rem;
    color: var(--color-muted);
    align-self: center;
    width: 1.25rem;
    text-align: center;
    transition: color 0.12s;
  }
  .eval-card[data-open="true"] .eval-card-toggle {
    color: var(--eval-color);
  }

  .eval-card-body {
    padding: 0 0.8rem 1rem;
    border-top: 1px dashed var(--color-border);
    margin-top: 0;
  }
  .eval-card-body-tabs {
    display: flex;
    justify-content: flex-end;
    padding: 0.4rem 0;
    border-bottom: 1px solid var(--color-border);
    margin-bottom: 0.85rem;
  }
  .eval-card-trace-link {
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.65rem;
    letter-spacing: 0.08em;
    color: var(--color-muted);
    text-decoration: none;
    padding: 0.2rem 0.5rem;
    border: 1px solid var(--color-border);
    transition: color 0.12s, border-color 0.12s;
  }
  .eval-card-trace-link:hover {
    color: var(--eval-color);
    border-color: var(--eval-color);
  }

  .eval-card-loading {
    padding: 1.5rem;
    text-align: center;
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.75rem;
    color: var(--color-muted);
  }
  .eval-card-error {
    color: var(--dir-opposes);
  }

  .eval-card-markdown {
    font-size: 0.88rem;
    line-height: 1.65;
    color: var(--color-foreground);
    max-width: 44rem;
  }
  .eval-card-markdown p {
    margin: 0 0 0.85em;
  }
  .eval-card-markdown h1,
  .eval-card-markdown h2,
  .eval-card-markdown h3 {
    font-weight: 600;
    letter-spacing: -0.01em;
    margin: 1.3em 0 0.4em;
    line-height: 1.3;
  }
  .eval-card-markdown h1 { font-size: 1.15em; }
  .eval-card-markdown h2 {
    font-size: 1em;
    padding-bottom: 0.2em;
    border-bottom: 1px solid var(--color-border);
  }
  .eval-card-markdown h3 { font-size: 0.95em; }
  .eval-card-markdown h1:first-child,
  .eval-card-markdown h2:first-child,
  .eval-card-markdown h3:first-child {
    margin-top: 0;
  }
  .eval-card-markdown ul,
  .eval-card-markdown ol {
    margin: 0 0 0.85em 1.4em;
    padding: 0;
  }
  .eval-card-markdown li {
    margin-bottom: 0.3em;
  }
  .eval-card-markdown code {
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.86em;
    background: var(--color-background);
    border: 1px solid var(--color-border);
    padding: 0.08em 0.35em;
    border-radius: 2px;
  }
  .eval-card-markdown pre {
    background: var(--color-background);
    border: 1px solid var(--color-border);
    padding: 0.7em 0.9em;
    overflow-x: auto;
    font-size: 0.78em;
    margin: 0 0 0.85em;
  }
  .eval-card-markdown pre code {
    background: none;
    border: none;
    padding: 0;
    font-size: 1em;
  }
  .eval-card-markdown blockquote {
    border-left: 3px solid var(--eval-color);
    margin: 0 0 0.85em;
    padding: 0.1em 0 0.1em 0.9em;
    font-style: italic;
    opacity: 0.85;
  }
  .eval-card-markdown table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.78rem;
    margin: 0 0 0.85em;
  }
  .eval-card-markdown th,
  .eval-card-markdown td {
    padding: 0.4em 0.6em;
    border: 1px solid var(--color-border);
    text-align: left;
  }
  .eval-card-markdown th {
    background: var(--color-background);
    font-weight: 600;
  }
  .eval-card-markdown a {
    color: var(--eval-color);
    text-decoration: underline;
    text-decoration-thickness: 1px;
    text-underline-offset: 2px;
  }
  .eval-card-markdown strong {
    font-weight: 600;
  }
  .eval-card-markdown hr {
    border: none;
    height: 1px;
    background: var(--color-border);
    margin: 1.5em 0;
  }

  .eval-card-followups {
    margin-top: 1.4rem;
    padding-top: 1rem;
    border-top: 1px dashed var(--color-border);
  }
  .eval-card-followups-label {
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.6rem;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: var(--color-muted);
    margin-bottom: 0.55rem;
  }
  .eval-card-followup-row {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
  }
  .eval-card-followup-btn {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    background: var(--color-background);
    border: 1px solid var(--color-border);
    padding: 0.45rem 0.75rem;
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.7rem;
    letter-spacing: 0.06em;
    color: var(--color-foreground);
    cursor: pointer;
    transition: all 0.12s ease;
  }
  .eval-card-followup-btn[data-kind="ground"]:hover:not(:disabled) {
    border-color: var(--eval-grounding);
    color: var(--eval-grounding);
    background: color-mix(in srgb, var(--eval-grounding) 6%, transparent);
  }
  .eval-card-followup-btn[data-kind="feedback"]:hover:not(:disabled) {
    border-color: var(--eval-feedback);
    color: var(--eval-feedback);
    background: color-mix(in srgb, var(--eval-feedback) 6%, transparent);
  }
  .eval-card-followup-btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
  .eval-card-followup-glyph {
    font-size: 0.85rem;
  }
  .eval-card-followup-btn[data-kind="ground"] .eval-card-followup-glyph {
    color: var(--eval-grounding);
  }
  .eval-card-followup-btn[data-kind="feedback"] .eval-card-followup-glyph {
    color: var(--eval-feedback);
  }
  .eval-card-followup-err {
    margin-top: 0.5rem;
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.7rem;
    color: var(--dir-opposes);
  }

  :root {
    --color-foreground: #111;
    --color-muted: #7a7a7a;
    --color-dim: #aaa;
    --color-border: #d8d4cf;
    --color-surface: #f5f2ec;
    --color-background: #fbfaf6;
    --color-accent: #333;

    --dir-supports: #3a7d44;
    --dir-opposes: #b04040;

    --eval-default: #4a4a4a;
    --eval-grounding: #a0522d;
    --eval-feedback: #2d7a7a;
  }

  @media (prefers-color-scheme: dark) {
    :root {
      --color-foreground: #ededed;
      --color-muted: #7a7a7a;
      --color-dim: #4a4a4a;
      --color-border: #2a2a2a;
      --color-surface: #131313;
      --color-background: #0b0b0b;
      --color-accent: #aaa;

      --dir-supports: #5aaa66;
      --dir-opposes: #d06060;

      --eval-default: #a0a0a0;
      --eval-grounding: #d49060;
      --eval-feedback: #5ec0b5;
    }
  }
`;
