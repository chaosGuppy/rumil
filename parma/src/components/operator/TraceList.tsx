"use client";

import { useState, useMemo } from "react";
import Link from "next/link";
import type { RunSummary } from "@/lib/operator-types";
import { CostBadge } from "./CostBadge";

function formatDuration(ms: number): string {
  if (ms === 0) return "\u2014";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

type RunTypeFilter = "all" | "chat" | "orchestrate";

export function TraceList({ runs }: { runs: RunSummary[] }) {
  const [typeFilter, setTypeFilter] = useState<RunTypeFilter>("all");

  const filtered = useMemo(
    () =>
      typeFilter === "all"
        ? runs
        : runs.filter((r) => r.run_type === typeFilter),
    [runs, typeFilter],
  );

  return (
    <div className="op-trace-list">
      <div className="op-trace-list-header">
        <h1 className="op-trace-list-title">Traces</h1>
        <div className="op-trace-list-filters">
          {(["all", "chat", "orchestrate"] as const).map((f) => (
            <button
              key={f}
              className={`op-filter-btn ${typeFilter === f ? "active" : ""}`}
              onClick={() => setTypeFilter(f)}
              type="button"
            >
              {f}
            </button>
          ))}
        </div>
      </div>

      {filtered.length === 0 ? (
        <div className="op-trace-list-empty">No runs found.</div>
      ) : (
        <div className="op-trace-list-items">
          {filtered.map((run) => (
            <Link
              key={run.id}
              href={`/traces/${run.id}`}
              className="op-trace-card"
            >
              <div className="op-trace-card-top">
                <span className={`op-run-type op-run-type-${run.run_type}`}>
                  {run.run_type}
                </span>
                <span className={`op-run-status op-run-status-${run.status}`}>
                  {run.status === "running" ? (
                    <span className="op-status-dot-animated" />
                  ) : null}
                  {run.status}
                </span>
                <span className="op-trace-card-time">
                  {formatTime(run.started_at)}
                </span>
              </div>

              <div className="op-trace-card-description">
                {run.description ?? run.id}
              </div>

              <div className="op-trace-card-stats">
                <span>{run.model_call_count} model calls</span>
                <span>{run.tool_call_count} tool calls</span>
                <span>
                  {compact(
                    run.total_usage.input_tokens +
                      run.total_usage.cache_read_tokens +
                      run.total_usage.cache_write_tokens,
                  )}{" "}
                  tokens in
                </span>
                <span>{formatDuration(run.duration_ms)}</span>
                <CostBadge cost={run.total_cost_usd} />
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
