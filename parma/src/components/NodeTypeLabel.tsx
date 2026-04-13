"use client";

import type { PageType } from "@/lib/types";

const TYPE_COLORS: Record<PageType, string> = {
  claim: "var(--node-claim)",
  question: "var(--node-question)",
  judgement: "var(--node-judgement)",
  concept: "var(--node-concept)",
  source: "var(--node-evidence)",
  wiki: "var(--node-context)",
  summary: "var(--node-context)",
};

export function NodeTypeLabel({ type }: { type: PageType }) {
  return (
    <span
      style={{
        fontFamily: "var(--font-mono-stack)",
        fontSize: "10px",
        letterSpacing: "0.06em",
        textTransform: "uppercase",
        color: TYPE_COLORS[type] ?? "var(--fg-dim)",
        userSelect: "none",
      }}
    >
      {type}
    </span>
  );
}

export function nodeColor(type: PageType): string {
  return TYPE_COLORS[type] ?? "var(--fg-dim)";
}
