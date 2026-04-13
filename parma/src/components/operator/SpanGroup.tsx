"use client";

import { useState, useCallback } from "react";
import type { TraceEvent } from "@/lib/operator-types";
import { CostBadge } from "./CostBadge";

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

function spanDuration(
  beginTs: string,
  endTs: string | undefined,
): number {
  if (!endTs) return 0;
  return new Date(endTs).getTime() - new Date(beginTs).getTime();
}

function spanCost(events: TraceEvent[]): number {
  return events.reduce(
    (s, e) => s + (e.event_type === "model" ? e.cost_usd : 0),
    0,
  );
}

export function SpanGroup({
  spanId,
  spanType,
  name,
  beginTimestamp,
  endTimestamp,
  children,
  events,
}: {
  spanId: string;
  spanType: string;
  name: string;
  beginTimestamp: string;
  endTimestamp?: string;
  children: React.ReactNode;
  events: TraceEvent[];
}) {
  const [expanded, setExpanded] = useState(true);
  const toggle = useCallback(() => setExpanded((v) => !v), []);

  const duration = spanDuration(beginTimestamp, endTimestamp);
  const cost = spanCost(events);
  const modelCount = events.filter((e) => e.event_type === "model").length;

  return (
    <div className={`op-span op-span-${spanType}`} data-span-id={spanId}>
      <button className="op-span-header" onClick={toggle} type="button">
        <span className="op-span-chevron">{expanded ? "\u25BC" : "\u25B6"}</span>
        <span className="op-span-type">{spanType}</span>
        <span className="op-span-name">{name}</span>
        <span className="op-span-meta">
          {modelCount > 0 && (
            <span>{modelCount} call{modelCount !== 1 ? "s" : ""}</span>
          )}
          {duration > 0 && <span>{formatDuration(duration)}</span>}
          {cost > 0 && <CostBadge cost={cost} />}
        </span>
      </button>
      {expanded && <div className="op-span-body">{children}</div>}
    </div>
  );
}
