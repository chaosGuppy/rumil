"use client";

import type { PageType } from "@/lib/types";

const TYPE_COLORS: Record<PageType, string> = {
  claim: "var(--node-claim)",
  question: "var(--node-question)",
  judgement: "var(--node-judgement)",
  source: "var(--node-evidence)",
  wiki: "var(--node-context)",
  view: "var(--node-context)",
  view_item: "var(--node-context)",
  view_meta: "var(--node-context)",
  artifact: "var(--node-claim)",
  inlay: "var(--node-context)",
  model: "var(--node-claim)",
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
