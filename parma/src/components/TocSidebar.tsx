"use client";

import type { WorldviewNode } from "@/lib/types";
import { nodeColor } from "./NodeTypeLabel";

interface TocSidebarProps {
  nodes: WorldviewNode[];
  activeIndex: number | null;
  questionHeadline: string;
  onNodeClick: (index: number) => void;
}

export function TocSidebar({
  nodes,
  activeIndex,
  questionHeadline,
  onNodeClick,
}: TocSidebarProps) {
  return (
    <nav className="toc">
      <div
        style={{
          marginBottom: "24px",
          paddingBottom: "16px",
          borderBottom: "1px solid var(--border)",
        }}
      >
        <div
          style={{
            fontFamily: "var(--font-mono-stack)",
            fontSize: "10px",
            letterSpacing: "0.1em",
            textTransform: "uppercase",
            color: "var(--fg-dim)",
            marginBottom: "8px",
          }}
        >
          Worldview
        </div>
        <div
          style={{
            fontSize: "14px",
            fontWeight: 500,
            lineHeight: 1.4,
            color: "var(--fg)",
          }}
        >
          {questionHeadline}
        </div>
      </div>

      <div
        style={{
          fontFamily: "var(--font-mono-stack)",
          fontSize: "10px",
          letterSpacing: "0.08em",
          textTransform: "uppercase",
          color: "var(--fg-dim)",
          marginBottom: "8px",
        }}
      >
        Findings
      </div>

      {nodes.map((node, i) => (
        <button
          key={i}
          className={`toc-item ${activeIndex === i ? "active" : ""}`}
          onClick={() => onNodeClick(i)}
        >
          <span
            className="type-dot"
            style={{ background: nodeColor(node.node_type) }}
          />
          {node.headline}
        </button>
      ))}
    </nav>
  );
}
