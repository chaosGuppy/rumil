"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { fetchProjects, fetchProjectRuns } from "@/lib/api";
import { useDocumentTitle } from "@/lib/useDocumentTitle";

// Evaluations listing page. Mirrors /ab-evals — polls every 5s so a run
// launched from /traces/{runId} shows up without manual refresh.
//
// Backend caveat: there's no dedicated /api/evaluations listing endpoint.
// We approximate by fanning out across projects, fetching each project's
// runs, and filtering rows whose `name` prefix matches an eval-family run
// ("evaluate", "grounding", or "feedback" — the prefixes used by the
// backend when it creates these runs). This is cheap — one GET per project
// — and doesn't require loading the full trace tree for every row.
//
// When the cheap prefix misses (e.g. a future run-name change), the row
// just silently drops off the list; the operator can still reach it via
// the main /traces page.

type EvalKind = "evaluate" | "grounding" | "feedback";

interface EvalRow {
  runId: string;
  projectId: string;
  projectName: string;
  kind: EvalKind;
  name: string;
  question: string | null;
  createdAt: string;
  costUsd: number | null;
  staged: boolean;
}

function classifyRunName(name: string): EvalKind | null {
  const n = name.toLowerCase();
  if (n.startsWith("evaluate")) return "evaluate";
  if (n.startsWith("grounding")) return "grounding";
  if (n.startsWith("feedback")) return "feedback";
  return null;
}

function readCostUsd(config: Record<string, unknown> | null): number | null {
  if (!config) return null;
  const v = config["total_cost_usd"];
  return typeof v === "number" ? v : null;
}

export default function EvaluationsPage() {
  const [rows, setRows] = useState<EvalRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [kindFilter, setKindFilter] = useState<EvalKind | "all">("all");

  useDocumentTitle(["evaluations"], "Rumil operator");

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      try {
        const projects = await fetchProjects();
        if (cancelled) return;
        // Fan out per-project in parallel; fetchProjectRuns returns in
        // most-recent-first order.
        const perProject = await Promise.all(
          projects.map(async (p) => {
            try {
              const runs = await fetchProjectRuns(p.id);
              return runs.map((r) => ({ run: r, project: p }));
            } catch {
              // A single broken project shouldn't nuke the whole page.
              return [];
            }
          }),
        );
        if (cancelled) return;

        const flattened: EvalRow[] = [];
        for (const group of perProject) {
          for (const { run, project } of group) {
            if (!run.run_id) continue;
            const kind = classifyRunName(run.name);
            if (!kind) continue;
            flattened.push({
              runId: run.run_id,
              projectId: project.id,
              projectName: project.name,
              kind,
              name: run.name,
              question: run.question_summary,
              createdAt: run.created_at,
              costUsd: readCostUsd(run.config ?? null),
              staged: run.staged,
            });
          }
        }
        flattened.sort((a, b) => b.createdAt.localeCompare(a.createdAt));
        if (!cancelled) setRows(flattened);
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "failed");
        }
      }
    };

    load();
    // Poll so a freshly-launched evaluation appears without a manual reload.
    const interval = window.setInterval(load, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, []);

  const filtered = useMemo(() => {
    if (!rows) return null;
    if (kindFilter === "all") return rows;
    return rows.filter((r) => r.kind === kindFilter);
  }, [rows, kindFilter]);

  if (error) return <div className="op-trace-list-empty">Error: {error}</div>;
  if (!rows) return <div className="op-trace-list-empty">Loading evaluations...</div>;

  const counts = {
    evaluate: rows.filter((r) => r.kind === "evaluate").length,
    grounding: rows.filter((r) => r.kind === "grounding").length,
    feedback: rows.filter((r) => r.kind === "feedback").length,
  };

  return (
    <div>
      <div className="op-trace-list-header">
        <div className="op-trace-list-title">Evaluations</div>
        <div className="op-trace-list-filters">
          {(["all", "evaluate", "grounding", "feedback"] as const).map((f) => (
            <button
              key={f}
              type="button"
              className={`op-filter-btn ${kindFilter === f ? "active" : ""}`}
              onClick={() => setKindFilter(f)}
            >
              {f}
              {f !== "all" && (
                <span className="op-eval-filter-count"> {counts[f]}</span>
              )}
            </button>
          ))}
        </div>
      </div>
      {filtered && filtered.length === 0 ? (
        <div className="op-trace-list-empty">
          {rows.length === 0
            ? "No evaluations yet. Open a trace and click 'evaluate'."
            : "No evaluations match this filter."}
        </div>
      ) : (
        <div className="op-trace-list-items">
          {filtered?.map((r) => (
            <Link
              key={r.runId}
              href={`/traces/${r.runId}`}
              className="op-trace-card op-eval-card"
            >
              <div className="op-trace-card-top">
                <span className={`op-run-type op-eval-kind op-eval-kind-${r.kind}`}>
                  {r.kind}
                </span>
                {r.staged && <span className="op-eval-staged">staged</span>}
                <span className="op-eval-project">{r.projectName}</span>
                <span className="op-trace-card-time">{formatWhen(r.createdAt)}</span>
              </div>
              {r.question && (
                <div className="op-trace-card-description">{r.question}</div>
              )}
              <div className="op-trace-card-stats">
                <span>{r.runId.slice(0, 8)}</span>
                {r.costUsd !== null && (
                  <>
                    <span>·</span>
                    <span>${r.costUsd.toFixed(2)}</span>
                  </>
                )}
                <span>·</span>
                <span className="op-eval-name">{r.name}</span>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

function formatWhen(iso: string): string {
  try {
    return new Date(iso).toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}
