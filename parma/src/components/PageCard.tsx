"use client";

import type { Page, PageLink } from "@/lib/types";
import { CredenceBadge } from "./CredenceBadge";
import { LinkBadges } from "./LinkBadges";
import { NodeTypeLabel } from "./NodeTypeLabel";
import { SourceBadge } from "./SourceBadge";
import { PageContent } from "./PageContent";
import { PageAnnotationActions } from "./PageAnnotationActions";
import { useInspectPanel } from "./InspectPanelContext";

interface PageCardProps {
  page: Page;
  links: PageLink[];
  onSelect?: () => void;
  onOpenSource?: (source: Page) => void;
  isActive?: boolean;
  isFocused?: boolean;
  activeDepth?: number;
}

function directionLabel(links: PageLink[]): string | null {
  for (const link of links) {
    if (link.link_type === "consideration" && link.direction && link.direction !== "neutral") {
      return link.direction;
    }
  }
  return null;
}

// Truncate an abstract without cutting a [8-hex] ref in half. If position
// `max` lands inside a `[...]` bracket, extend forward to the nearest closing
// `]` so the ref regex in PageContent can still match. Otherwise pull back to
// the nearest whitespace for a natural word boundary.
function truncateAbstract(text: string, max: number): string {
  if (text.length <= max) return text;
  const openIdx = text.lastIndexOf("[", max - 1);
  const closeIdx = text.lastIndexOf("]", max - 1);
  if (openIdx > closeIdx) {
    const nextClose = text.indexOf("]", max);
    if (nextClose !== -1 && nextClose - max < 40) {
      return text.slice(0, nextClose + 1) + "…";
    }
    return text.slice(0, openIdx).trimEnd() + "…";
  }
  const cutAt = text.lastIndexOf(" ", max);
  const end = cutAt > max - 40 ? cutAt : max;
  return text.slice(0, end).trimEnd() + "…";
}

export function PageCard({
  page,
  links,
  onSelect,
  onOpenSource,
  isActive,
  isFocused,
  activeDepth = 0,
}: PageCardProps) {
  const direction = directionLabel(links);
  const citedSourceIds = links
    .filter((l) => l.link_type === "cites")
    .map((l) => l.to_page_id.slice(0, 8));

  const className = [
    "node-card",
    isActive ? "node-active" : "",
    isFocused ? "node-focused" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      className={className}
      data-type={page.page_type}
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
        <NodeTypeLabel type={page.page_type} />
        {page.importance !== null && page.importance > 0 && (
          <span
            style={{
              fontFamily: "var(--font-mono-stack)",
              fontSize: "10px",
              color: page.importance >= 3 ? "var(--fg-dim)" : "var(--fg-muted)",
              letterSpacing: "0.02em",
              opacity: page.importance >= 4 ? 0.5 : 1,
            }}
          >
            L{page.importance}
          </span>
        )}
        {direction && (
          <span
            style={{
              fontFamily: "var(--font-mono-stack)",
              fontSize: "10px",
              letterSpacing: "0.04em",
              color: direction === "supports" ? "var(--link-supports)" : "var(--link-opposes)",
            }}
          >
            {direction}
          </span>
        )}
        <CredenceBadge credence={page.credence} robustness={page.robustness} />
        <SourceBadge sourceIds={citedSourceIds} onOpenDrawer={onOpenSource} />
        <LinkBadges links={links} />
        <PageAnnotationActions pageId={page.id} />
        {page.provenance_call_type && page.provenance_call_type !== "system" && (
          <ProvenanceChip page={page} />
        )}
      </div>

      <h3
        style={{
          fontSize: "19px",
          fontWeight: 500,
          lineHeight: 1.35,
          margin: "0 0 6px 0",
          color: "var(--fg)",
          fontFamily: "var(--font-body-stack)",
          cursor: onSelect ? "pointer" : undefined,
        }}
        onClick={onSelect}
      >
        {page.headline}
      </h3>

      <PageContent
        text={page.content}
        pageId={page.id}
      />

      {page.abstract && page.abstract !== page.content && (
        <div
          style={{
            marginTop: "8px",
            fontSize: "13px",
            color: "var(--fg-muted)",
            fontStyle: "italic",
            lineHeight: 1.5,
          }}
        >
          <PageContent
            text={truncateAbstract(page.abstract, 200)}
            inline
            className=""
          />
        </div>
      )}
    </div>
  );
}

// Provenance chip in the PageCard meta row. Used to be a static label
// "via {call_type}"; now when the page carries a run_id it becomes a
// button that jumps into TRACE view mode with the producing call
// pre-selected. Falls back to the static label if run_id is missing
// (older pages written before run_id was captured).
function ProvenanceChip({ page }: { page: Page }) {
  const { openTrace } = useInspectPanel();
  const runId = page.run_id;
  const callId = page.provenance_call_id;

  const baseStyle: React.CSSProperties = {
    fontFamily: "var(--font-mono-stack)",
    fontSize: "9px",
    color: "var(--fg-dim)",
    letterSpacing: "0.04em",
    opacity: 0.7,
  };

  if (!runId) {
    return <span style={baseStyle}>via {page.provenance_call_type}</span>;
  }

  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        openTrace(runId, callId);
      }}
      style={{
        ...baseStyle,
        background: "none",
        border: "none",
        padding: 0,
        cursor: "pointer",
        textDecoration: "underline",
        textUnderlineOffset: "2px",
        textDecorationColor: "var(--border)",
      }}
      title={`View trace (run ${runId.slice(0, 8)})`}
    >
      via {page.provenance_call_type}
    </button>
  );
}
