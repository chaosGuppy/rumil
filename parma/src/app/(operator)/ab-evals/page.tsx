"use client";

import { useEffect, useState } from "react";
import { fetchAbEvals } from "@/lib/api";
import type { ABEvalReportListItem } from "@/lib/api";
import { useDocumentTitle } from "@/lib/useDocumentTitle";

// Minimal ab-evals listing page. The AB-eval launcher in RunPicker lands
// the user here after firing a background eval — since the backend
// returns 202 before the report exists, there's nothing to deep-link to
// yet. Polling every 5s picks up the new row once the background task
// finishes.

export default function ABEvalsPage() {
  const [evals, setEvals] = useState<ABEvalReportListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useDocumentTitle(["ab-evals"], "Rumil operator");

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      fetchAbEvals()
        .then((rows) => {
          if (!cancelled) setEvals(rows);
        })
        .catch((e) => {
          if (!cancelled) setError(e?.message ?? "failed");
        });
    };
    load();
    // Poll so a freshly-launched eval shows up without a manual reload.
    const interval = window.setInterval(load, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, []);

  if (error) return <div className="op-trace-list-empty">Error: {error}</div>;
  if (!evals) return <div className="op-trace-list-empty">Loading ab-evals...</div>;

  return (
    <div>
      <div className="op-trace-list-header">
        <div className="op-trace-list-title">A/B evaluations</div>
      </div>
      {evals.length === 0 ? (
        <div className="op-trace-list-empty">
          No A/B evaluations yet. Launch one from the trace run picker.
        </div>
      ) : (
        <div className="op-trace-list-items">
          {evals.map((e) => (
            <div key={e.id} className="op-trace-card">
              <div className="op-trace-card-top">
                <span className="op-run-type">ab-eval</span>
                <span className="op-trace-card-time">{formatWhen(e.created_at)}</span>
              </div>
              {e.question_headline && (
                <div className="op-trace-card-description">{e.question_headline}</div>
              )}
              <div className="op-trace-card-stats">
                <span>A: {e.run_id_a.slice(0, 8)}</span>
                <span>·</span>
                <span>B: {e.run_id_b.slice(0, 8)}</span>
                {e.preferences.length > 0 && (
                  <>
                    <span>·</span>
                    <span>{e.preferences.length} dimensions</span>
                  </>
                )}
              </div>
              {e.overall_assessment_preview && (
                <div className="ab-evals-preview">
                  {e.overall_assessment_preview}
                </div>
              )}
            </div>
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
