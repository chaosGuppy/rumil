"use client";

import type { Page, PageLink } from "@/lib/types";
import { CredenceBadge } from "./CredenceBadge";
import { LinkBadges } from "./LinkBadges";
import { NodeTypeLabel } from "./NodeTypeLabel";
import { SourceBadge } from "./SourceBadge";
import { PageContent } from "./PageContent";

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

  if (page.page_type === "concept") {
    return (
      <div
        className={[
          "concept-node-card",
          isFocused ? "node-focused" : "",
        ]
          .filter(Boolean)
          .join(" ")}
      >
        <div className="concept-node-label">concept</div>
        <h3>{page.headline}</h3>
        <PageContent text={page.content} excludeConceptId={page.id} />
      </div>
    );
  }

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
        {page.provenance_call_type && page.provenance_call_type !== "system" && (
          <span
            style={{
              fontFamily: "var(--font-mono-stack)",
              fontSize: "9px",
              color: "var(--fg-dim)",
              letterSpacing: "0.04em",
              opacity: 0.7,
            }}
          >
            via {page.provenance_call_type}
          </span>
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

      <PageContent text={page.content} excludeConceptId={page.id} />

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
          {page.abstract.length > 200 ? page.abstract.slice(0, 200) + "..." : page.abstract}
        </div>
      )}
    </div>
  );
}
