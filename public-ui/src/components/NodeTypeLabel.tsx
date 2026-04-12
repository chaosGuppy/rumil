"use client";

import type { WorldviewNodeType } from "@/lib/types";

const TYPE_COLORS: Record<WorldviewNodeType, string> = {
  claim: "var(--node-claim)",
  hypothesis: "var(--node-hypothesis)",
  evidence: "var(--node-evidence)",
  uncertainty: "var(--node-uncertainty)",
  context: "var(--node-context)",
};

const TYPE_LABELS: Record<WorldviewNodeType, string> = {
  claim: "claim",
  hypothesis: "hypothesis",
  evidence: "evidence",
  uncertainty: "uncertainty",
  context: "context",
};

export function NodeTypeLabel({ type }: { type: WorldviewNodeType }) {
  return (
    <span
      style={{
        fontFamily: "var(--font-mono-stack)",
        fontSize: "10px",
        letterSpacing: "0.06em",
        textTransform: "uppercase",
        color: TYPE_COLORS[type],
        userSelect: "none",
      }}
    >
      {TYPE_LABELS[type]}
    </span>
  );
}

export function nodeColor(type: WorldviewNodeType): string {
  return TYPE_COLORS[type];
}
