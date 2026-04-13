import type { RunSummary } from "@/lib/operator-types";
import { TokenBar } from "./TokenBar";
import { CostBadge } from "./CostBadge";

function formatDuration(ms: number): string {
  if (ms === 0) return "running";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function RunHeader({ run }: { run: RunSummary }) {
  return (
    <div className="op-run-header">
      <div className="op-run-header-top">
        <div className="op-run-header-left">
          <span className={`op-run-type op-run-type-${run.run_type}`}>
            {run.run_type}
          </span>
          <span className={`op-run-status op-run-status-${run.status}`}>
            {run.status}
          </span>
        </div>
        <div className="op-run-header-right">
          <span className="op-run-id">{run.id}</span>
        </div>
      </div>

      {run.description && (
        <div className="op-run-description">{run.description}</div>
      )}
      {run.scope_node_headline && (
        <div className="op-run-scope">
          scope: {run.scope_node_headline}
        </div>
      )}

      <div className="op-run-stats">
        <div className="op-run-stat">
          <span className="op-run-stat-label">started</span>
          <span className="op-run-stat-value">{formatTime(run.started_at)}</span>
        </div>
        <div className="op-run-stat">
          <span className="op-run-stat-label">duration</span>
          <span className="op-run-stat-value">{formatDuration(run.duration_ms)}</span>
        </div>
        <div className="op-run-stat">
          <span className="op-run-stat-label">model calls</span>
          <span className="op-run-stat-value">{run.model_call_count}</span>
        </div>
        <div className="op-run-stat">
          <span className="op-run-stat-label">tool calls</span>
          <span className="op-run-stat-value">{run.tool_call_count}</span>
        </div>
        <div className="op-run-stat">
          <span className="op-run-stat-label">cost</span>
          <span className="op-run-stat-value">
            <CostBadge cost={run.total_cost_usd} />
          </span>
        </div>
      </div>

      <div className="op-run-tokens">
        <TokenBar usage={run.total_usage} showLabels />
      </div>
    </div>
  );
}
