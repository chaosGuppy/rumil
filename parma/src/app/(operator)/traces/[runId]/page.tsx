"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { TraceDetail } from "@/components/operator/TraceDetail";
import { fetchRunDetail } from "@/lib/operator-api";
import {
  fetchCall,
  fetchRunTraceTree,
  startEvaluation,
  startFeedbackPipeline,
  startGroundPipeline,
  type CallDetail,
  type EvalType,
  type RunTraceTree,
} from "@/lib/api";
import { useDocumentTitle } from "@/lib/useDocumentTitle";
import type { RunDetail } from "@/lib/operator-types";

// The operator's run detail page. In addition to the normal trace timeline,
// this page now hosts the evaluations-UI launchers — the spec's Option 3.
// We cross-reference the rumil-side trace tree (fetchRunTraceTree) to
// detect the root call type:
//   - EVALUATE root → show "Run grounding pipeline" / "Run feedback pipeline"
//     buttons, and render the evaluation markdown from review_json.evaluation
//     at the top of the view.
//   - Non-eval root with a question_id → show "Evaluate this question" +
//     eval-type picker.
// This keeps evaluations tightly coupled to trace inspection (the natural
// moment to act) and avoids cluttering the ab-evals pattern with another
// checkbox-driven workflow.

type EvalContext =
  | { kind: "loading" }
  | { kind: "none"; reason?: string }
  | {
      kind: "evaluate-run";
      rootCallId: string;
      questionId: string | null;
      questionHeadline: string | null;
    }
  | {
      kind: "non-eval-run";
      questionId: string;
      questionHeadline: string | null;
    };

export default function TraceDetailPage() {
  const { runId } = useParams<{ runId: string }>();
  const router = useRouter();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [evalContext, setEvalContext] = useState<EvalContext>({ kind: "loading" });
  const [evalCall, setEvalCall] = useState<CallDetail | null>(null);

  const titleHeadline =
    run?.description ??
    run?.scope_node_headline ??
    (runId ? `run ${runId.slice(0, 8)}` : null);
  useDocumentTitle(
    ["trace", titleHeadline, run?.workspace_name],
    "Rumil operator",
  );

  useEffect(() => {
    fetchRunDetail(runId)
      .then((data) => {
        setRun(data);
        setLoading(false);
      })
      .catch((e) => {
        setError(e.message);
        setLoading(false);
      });
  }, [runId]);

  // Resolve the rumil-side trace tree to learn the root call_type. If the
  // runId isn't known to rumil (e.g. legacy parma-only runs), we just hide
  // the launcher — no need to surface an error.
  useEffect(() => {
    let cancelled = false;
    setEvalContext({ kind: "loading" });
    setEvalCall(null);
    fetchRunTraceTree(runId)
      .then(async (tree: RunTraceTree) => {
        if (cancelled) return;
        const root =
          tree.calls.find((c) => !c.call.parent_call_id) ?? tree.calls[0];
        if (!root) {
          setEvalContext({ kind: "none", reason: "no root call" });
          return;
        }
        const questionHeadline = tree.question?.headline ?? null;
        const questionId = tree.question?.id ?? null;
        if (root.call.call_type === "evaluate") {
          setEvalContext({
            kind: "evaluate-run",
            rootCallId: root.call.id,
            questionId,
            questionHeadline,
          });
          // Pull the full Call so we can surface review_json.evaluation.
          try {
            const call = await fetchCall(root.call.id);
            if (!cancelled) setEvalCall(call);
          } catch {
            // Silent — the pipeline buttons still work without the text.
          }
          return;
        }
        if (questionId) {
          setEvalContext({
            kind: "non-eval-run",
            questionId,
            questionHeadline,
          });
        } else {
          setEvalContext({ kind: "none", reason: "no question" });
        }
      })
      .catch(() => {
        if (!cancelled) setEvalContext({ kind: "none", reason: "not a rumil run" });
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  if (loading) return <div className="op-trace-list-empty">Loading trace...</div>;
  if (error)
    return (
      <div className="op-trace-list-empty">
        Error: {error}
        <br />
        <Link href="/traces" style={{ color: "var(--accent)" }}>
          Back to traces
        </Link>
      </div>
    );
  if (!run)
    return (
      <div className="op-trace-list-empty">
        Run not found: {runId}
        <br />
        <Link href="/traces" style={{ color: "var(--accent)" }}>
          Back to traces
        </Link>
      </div>
    );

  const evaluationText = extractEvaluationText(evalCall?.review_json);

  return (
    <div>
      <Link href="/traces" className="op-back-link">
        &larr; traces
      </Link>
      {evalContext.kind === "evaluate-run" && evaluationText && (
        <EvaluationReport
          text={evaluationText}
          questionHeadline={evalContext.questionHeadline}
        />
      )}
      <EvalLauncher
        context={evalContext}
        onNavigate={(nextRunId) => router.push(`/traces/${nextRunId}`)}
      />
      <TraceDetail run={run} />
    </div>
  );
}

function extractEvaluationText(review?: Record<string, unknown>): string | null {
  if (!review) return null;
  const raw = review["evaluation"];
  return typeof raw === "string" && raw.trim().length > 0 ? raw : null;
}

// Evaluation-report panel. Rendered above the normal trace when the run is
// an EVALUATE whose review_json carries the rendered markdown. The
// aesthetic mirrors op-run-header — muted, utilitarian, reads like a
// plant-operator printout, not a marketing page.
function EvaluationReport({
  text,
  questionHeadline,
}: {
  text: string;
  questionHeadline: string | null;
}) {
  return (
    <section className="op-eval-report">
      <header className="op-eval-report-head">
        <span className="op-eval-report-tag">evaluation</span>
        {questionHeadline && (
          <span className="op-eval-report-q" title={questionHeadline}>
            {questionHeadline}
          </span>
        )}
      </header>
      <div className="op-eval-report-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
      </div>
    </section>
  );
}

function EvalLauncher({
  context,
  onNavigate,
}: {
  context: EvalContext;
  onNavigate: (runId: string) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const [evalType, setEvalType] = useState<EvalType>("default");

  const handleEvaluate = useCallback(async () => {
    if (context.kind !== "non-eval-run" || busy) return;
    setBusy("evaluate");
    setErrMsg(null);
    try {
      const res = await startEvaluation(context.questionId, evalType);
      onNavigate(res.run_id);
    } catch (e) {
      setErrMsg(e instanceof Error ? e.message : "Could not start evaluation");
      setBusy(null);
    }
  }, [context, evalType, busy, onNavigate]);

  const handleGround = useCallback(async () => {
    if (context.kind !== "evaluate-run" || busy) return;
    setBusy("ground");
    setErrMsg(null);
    try {
      const res = await startGroundPipeline(context.rootCallId, 1);
      onNavigate(res.run_id);
    } catch (e) {
      setErrMsg(
        e instanceof Error ? e.message : "Could not start grounding pipeline",
      );
      setBusy(null);
    }
  }, [context, busy, onNavigate]);

  const handleFeedback = useCallback(async () => {
    if (context.kind !== "evaluate-run" || busy) return;
    setBusy("feedback");
    setErrMsg(null);
    try {
      const res = await startFeedbackPipeline(context.rootCallId, 1);
      onNavigate(res.run_id);
    } catch (e) {
      setErrMsg(
        e instanceof Error ? e.message : "Could not start feedback pipeline",
      );
      setBusy(null);
    }
  }, [context, busy, onNavigate]);

  if (context.kind === "loading" || context.kind === "none") return null;

  return (
    <section className="op-eval-launch">
      {context.kind === "evaluate-run" ? (
        <>
          <div className="op-eval-launch-label">
            <span className="op-eval-launch-dot" aria-hidden />
            this run produced an evaluation — drive a cleanup pipeline from it
          </div>
          <div className="op-eval-launch-actions">
            <button
              type="button"
              className="op-eval-launch-btn"
              onClick={handleGround}
              disabled={busy !== null}
            >
              {busy === "ground" ? "launching…" : "run grounding pipeline →"}
            </button>
            <button
              type="button"
              className="op-eval-launch-btn"
              onClick={handleFeedback}
              disabled={busy !== null}
            >
              {busy === "feedback" ? "launching…" : "run feedback pipeline →"}
            </button>
          </div>
        </>
      ) : (
        <>
          <div className="op-eval-launch-label">
            <span className="op-eval-launch-dot" aria-hidden />
            evaluate this question
            {context.questionHeadline && (
              <span className="op-eval-launch-q"> — {context.questionHeadline}</span>
            )}
          </div>
          <div className="op-eval-launch-actions">
            <div
              className="op-eval-launch-select"
              role="group"
              aria-label="Evaluation type"
            >
              {(["default", "grounding", "feedback"] as const).map((t) => (
                <button
                  key={t}
                  type="button"
                  className={
                    "op-eval-launch-select-btn " +
                    (evalType === t ? "is-active" : "")
                  }
                  onClick={() => setEvalType(t)}
                  disabled={busy !== null}
                >
                  {t}
                </button>
              ))}
            </div>
            <button
              type="button"
              className="op-eval-launch-btn"
              onClick={handleEvaluate}
              disabled={busy !== null}
            >
              {busy === "evaluate" ? "launching…" : "evaluate →"}
            </button>
          </div>
        </>
      )}
      {errMsg && <div className="op-eval-launch-err">{errMsg}</div>}
    </section>
  );
}

// Lightweight module-local typing for the rumil trace tree's nested fields.
// The actual type comes from fetchRunTraceTree — these helpers just narrow
// the shape we care about.
export type { RunTraceTree };
