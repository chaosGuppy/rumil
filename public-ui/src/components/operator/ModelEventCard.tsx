"use client";

import { useState, useCallback } from "react";
import type { ModelEvent } from "@/lib/operator-types";
import { TokenBar } from "./TokenBar";
import { CostBadge } from "./CostBadge";
import { MessageInspector } from "./MessageInspector";

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  if (n >= 1_000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

const TABS = ["input", "output", "tokens"] as const;
type Tab = (typeof TABS)[number];

export function ModelEventCard({ event }: { event: ModelEvent }) {
  const [expanded, setExpanded] = useState(false);
  const [activeTab, setActiveTab] = useState<Tab>("input");
  const toggle = useCallback(() => setExpanded((v) => !v), []);

  const totalIn =
    event.usage.input_tokens +
    event.usage.cache_read_tokens +
    event.usage.cache_write_tokens;

  const toolCallCount = event.output_content.filter(
    (b) => b.type === "tool_use",
  ).length;

  return (
    <div className="op-model-card">
      <button className="op-model-header" onClick={toggle} type="button">
        <span className="op-model-icon">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <circle cx="6" cy="6" r="5" stroke="currentColor" strokeWidth="1.5" />
            <circle cx="6" cy="6" r="2" fill="currentColor" />
          </svg>
        </span>
        <span className="op-model-name">{event.config.model.replace("claude-", "")}</span>
        <span className="op-model-meta">
          {compact(totalIn)} &rarr; {compact(event.usage.output_tokens)}
        </span>
        {toolCallCount > 0 && (
          <span className="op-model-tools">
            {toolCallCount} tool{toolCallCount !== 1 ? "s" : ""}
          </span>
        )}
        <span className="op-model-stop">{event.stop_reason}</span>
        <span className="op-model-duration">{formatDuration(event.duration_ms)}</span>
        <CostBadge cost={event.cost_usd} />
        <span className="op-model-chevron">{expanded ? "\u25BC" : "\u25B6"}</span>
      </button>

      {expanded && (
        <div className="op-model-body">
          <div className="op-model-config">
            <span>temp {event.config.temperature}</span>
            <span>max {compact(event.config.max_tokens)}</span>
            <span>{event.tools_offered.length} tools</span>
          </div>
          <div className="op-model-tabs">
            {TABS.map((tab) => (
              <button
                key={tab}
                className={`op-model-tab ${activeTab === tab ? "active" : ""}`}
                onClick={() => setActiveTab(tab)}
                type="button"
              >
                {tab}
              </button>
            ))}
          </div>
          <div className="op-model-tab-content">
            {activeTab === "input" && (
              <MessageInspector messages={event.input_messages} />
            )}
            {activeTab === "output" && (
              <div className="op-model-output">
                {event.output_content.map((block, i) => {
                  if (block.type === "text") {
                    return (
                      <pre key={i} className="op-msg-text">
                        {block.text}
                      </pre>
                    );
                  }
                  if (block.type === "tool_use") {
                    return (
                      <div key={i} className="op-msg-tool-use">
                        <div className="op-msg-tool-use-header">
                          <span className="op-msg-tool-name">{block.name}</span>
                          <span className="op-msg-tool-id">{block.id}</span>
                        </div>
                        <pre className="op-msg-tool-input">
                          {JSON.stringify(block.input, null, 2)}
                        </pre>
                      </div>
                    );
                  }
                  return null;
                })}
              </div>
            )}
            {activeTab === "tokens" && (
              <TokenBar usage={event.usage} showLabels />
            )}
          </div>
        </div>
      )}
    </div>
  );
}
