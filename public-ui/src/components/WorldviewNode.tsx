"use client";

import type { WorldviewNode as WorldviewNodeType } from "@/lib/types";
import { CredenceBadge } from "./CredenceBadge";
import { NodeTypeLabel } from "./NodeTypeLabel";

interface WorldviewNodeProps {
  node: WorldviewNodeType;
  index: number;
  onExpandPane?: (node: WorldviewNodeType, index: number) => void;
  isActive?: boolean;
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

function ProvenanceIndicator({ count }: { count: number }) {
  if (count === 0) return null;
  return (
    <span
      title={`${count} source${count > 1 ? "s" : ""} referenced`}
      style={{
        fontFamily: "var(--font-mono-stack)",
        fontSize: "10px",
        color: "var(--fg-dim)",
        letterSpacing: "0.02em",
      }}
    >
      {count} src
    </span>
  );
}

export function WorldviewNodeCard({
  node,
  index,
  onExpandPane,
  isActive,
  activeDepth = 0,
}: WorldviewNodeProps) {
  const hasChildren = node.children.length > 0;

  return (
    <div
      className={`node-card fade-in fade-in-delay-${Math.min(index + 1, 5)}${isActive ? " node-active" : ""}`}
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
        <CredenceBadge credence={node.credence} robustness={node.robustness} />
        <ProvenanceIndicator count={node.source_page_ids.length} />
      </div>

      <h3
        style={{
          fontSize: "19px",
          fontWeight: 500,
          lineHeight: 1.35,
          margin: "0 0 6px 0",
          color: "var(--fg)",
          fontFamily: "var(--font-body-stack)",
        }}
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
