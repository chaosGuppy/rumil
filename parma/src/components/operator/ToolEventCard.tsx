"use client";

import { useState, useCallback } from "react";
import type { ToolEvent } from "@/lib/operator-types";

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export function ToolEventCard({ event }: { event: ToolEvent }) {
  const [expanded, setExpanded] = useState(false);
  const toggle = useCallback(() => setExpanded((v) => !v), []);

  const argPreview = JSON.stringify(event.arguments);
  const truncatedArgs =
    argPreview.length > 80 ? argPreview.slice(0, 77) + "\u2026" : argPreview;

  return (
    <div className={`op-tool-card ${event.error ? "op-tool-error" : ""}`}>
      <button className="op-tool-header" onClick={toggle} type="button">
        <span className="op-tool-icon">→</span>
        <span className="op-tool-name">{event.function_name}</span>
        {!expanded && (
          <span className="op-tool-args-preview">{truncatedArgs}</span>
        )}
        <span className="op-tool-duration">{formatDuration(event.duration_ms)}</span>
        {event.error && <span className="op-tool-error-badge">error</span>}
        <span className="op-tool-chevron">{expanded ? "\u25BC" : "\u25B6"}</span>
      </button>
      {expanded && (
        <div className="op-tool-body">
          <div className="op-tool-section">
            <div className="op-tool-section-label">arguments</div>
            <pre className="op-tool-pre">
              {JSON.stringify(event.arguments, null, 2)}
            </pre>
          </div>
          <div className="op-tool-section">
            <div className="op-tool-section-label">result</div>
            <pre className="op-tool-pre">{event.result}</pre>
          </div>
          {event.error && (
            <div className="op-tool-section">
              <div className="op-tool-section-label op-tool-error-label">error</div>
              <pre className="op-tool-pre op-tool-error-text">{event.error}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
