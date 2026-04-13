"use client";

import type { WorldviewNode as WorldviewNodeType } from "@/lib/types";
import type { SourceFull } from "@/lib/api";
import { CredenceBadge } from "./CredenceBadge";
import { NodeTypeLabel } from "./NodeTypeLabel";
import { SourceBadge } from "./SourceBadge";

interface WorldviewNodeProps {
  node: WorldviewNodeType;
  index: number;
  onExpandPane?: (node: WorldviewNodeType, index: number) => void;
  onFocus?: (nodeId: string) => void;
  onOpenSource?: (source: SourceFull) => void;
  isActive?: boolean;
  isFocused?: boolean;
  activeDepth?: number;
}

function ChevronRight() {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ width: "12px", height: "12px" }}
    >
      <path d="M6 4l4 4-4 4" />
    </svg>
  );
}


export function WorldviewNodeCard({
  node,
  index,
  onExpandPane,
  onFocus,
  onOpenSource,
  isActive,
  isFocused,
  activeDepth = 0,
}: WorldviewNodeProps) {
  const hasChildren = node.children.length > 0;

  const className = [
    "node-card",
    "fade-in",
    `fade-in-delay-${Math.min(index + 1, 5)}`,
    isActive ? "node-active" : "",
    isFocused ? "node-focused" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      className={className}
      data-type={node.node_type}
      style={isActive ? { "--active-tint": `var(--active-${activeDepth % 5})` } as React.CSSProperties : undefined}
    >
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          gap: "10px",
          marginBottom: "6px",
        }}
      >
        <NodeTypeLabel type={node.node_type} />
        {node.importance !== undefined && node.importance > 0 && (
          <span
            style={{
              fontFamily: "var(--font-mono-stack)",
              fontSize: "10px",
              color: node.importance >= 3 ? "var(--fg-dim)" : "var(--fg-muted)",
              letterSpacing: "0.02em",
              opacity: node.importance >= 4 ? 0.5 : 1,
            }}
          >
            L{node.importance}
          </span>
        )}
        <CredenceBadge credence={node.credence} robustness={node.robustness} />
        <SourceBadge sourceIds={node.source_page_ids} onOpenDrawer={onOpenSource} />
      </div>

      <h3
        style={{
          fontSize: "19px",
          fontWeight: 500,
          lineHeight: 1.35,
          margin: "0 0 6px 0",
          color: "var(--fg)",
          fontFamily: "var(--font-body-stack)",
          cursor: onFocus ? "pointer" : undefined,
        }}
        onClick={onFocus ? () => onFocus(node.headline) : undefined}
      >
        {node.headline}
      </h3>

      <div className="worldview-prose">
        <p style={{ margin: 0, fontSize: "15px" }}>{node.content}</p>
      </div>

      {hasChildren && onExpandPane && (
        <button
          className="expand-trigger"
          onClick={() => onExpandPane(node, index)}
          style={{ marginTop: "10px" }}
        >
          <span>
            {node.children.length} detail
            {node.children.length > 1 ? "s" : ""}
          </span>
          <ChevronRight />
        </button>
      )}
    </div>
  );
}
