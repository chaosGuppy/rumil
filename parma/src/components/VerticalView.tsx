"use client";

import { useState, useEffect, useCallback, useImperativeHandle, forwardRef } from "react";
import type { QuestionView, ViewItem } from "@/lib/types";
import { CredenceBadge } from "./CredenceBadge";
import { LinkBadges } from "./LinkBadges";
import { NodeTypeLabel } from "./NodeTypeLabel";
import { SourceBadge } from "./SourceBadge";
import { PageContent } from "./PageContent";
import { PageAnnotationActions } from "./PageAnnotationActions";

interface VerticalViewProps {
  view: QuestionView;
  focusNodeId?: string | null;
  onFocusHandled?: () => void;
}

const SECTION_LABELS: Record<string, string> = {
  current_position: "Current Position",
  core_findings: "Core Findings",
  live_hypotheses: "Live Hypotheses",
  key_evidence: "Key Evidence",
  key_uncertainties: "Key Uncertainties",
  structural_framing: "Structural Framing",
  supporting_detail: "Supporting Detail",
  promotion_candidates: "Promotion Candidates",
  demotion_candidates: "Demotion Candidates",
};

function VerticalItem({
  item,
  expanded,
  onToggle,
  onFocus,
  focusedId,
}: {
  item: ViewItem;
  expanded: boolean;
  onToggle: () => void;
  onFocus: (headline: string) => void;
  focusedId: string | null;
}) {
  const page = item.page;
  const isFocused = focusedId ? page.headline.includes(focusedId) : false;
  const direction = item.links.find(
    (l) => l.link_type === "consideration" && l.direction && l.direction !== "neutral",
  )?.direction;
  const citedSourceIds = item.links
    .filter((l) => l.link_type === "cites")
    .map((l) => l.to_page_id.slice(0, 8));

  return (
    <div
      className={`vertical-node ${isFocused ? "node-focused" : ""}`}
      data-page-id={page.id}
    >
      <div className="vertical-node-inner">
        <div className="vertical-node-header">
          <NodeTypeLabel type={page.page_type} />
          <span
            className="vertical-headline"
            onClick={() => {
              onToggle();
              onFocus(page.headline);
            }}
          >
            {page.headline}
          </span>
          {direction && (
            <span
              style={{
                fontFamily: "var(--font-mono-stack)",
                fontSize: "10px",
                color: direction === "supports" ? "var(--link-supports)" : "var(--link-opposes)",
              }}
            >
              {direction}
            </span>
          )}
          <CredenceBadge credence={page.credence} robustness={page.robustness} />
          <SourceBadge sourceIds={citedSourceIds} />
          <LinkBadges links={item.links} />
          {page.importance !== null && page.importance > 0 && (
            <span className="vertical-label-dim">L{page.importance}</span>
          )}
          <PageAnnotationActions pageId={page.id} />
        </div>

        {expanded && (
          <div className="vertical-content">
            <PageContent
              text={page.content}
              pageId={page.id}
            />
          </div>
        )}
      </div>
    </div>
  );
}

export interface VerticalViewHandle {
  expandAll: () => void;
  collapseAll: () => void;
}

export const VerticalView = forwardRef<VerticalViewHandle, VerticalViewProps>(
  function VerticalView({ view, focusNodeId, onFocusHandled }, ref) {
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const [expandedSections, setExpandedSections] = useState<Set<string>>(
    () => new Set(view.sections.map((s) => s.name)),
  );
  const [expandedItems, setExpandedItems] = useState<Set<string>>(new Set());

  const expandAll = useCallback(() => {
    setExpandedSections(new Set(view.sections.map((s) => s.name)));
    const allIds = view.sections.flatMap((s) => s.items.map((i) => i.page.id));
    setExpandedItems(new Set(allIds));
  }, [view]);

  const collapseAll = useCallback(() => {
    setExpandedSections(new Set());
    setExpandedItems(new Set());
  }, []);

  useImperativeHandle(ref, () => ({ expandAll, collapseAll }), [expandAll, collapseAll]);

  const toggleSection = useCallback((name: string) => {
    setExpandedSections((prev) => {
      const next = new Set(prev);
      if (next.has(name)) {
        next.delete(name);
      } else {
        next.add(name);
      }
      return next;
    });
  }, []);

  const toggleItem = useCallback((id: string) => {
    setExpandedItems((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

  useEffect(() => {
    if (!focusNodeId) return;
    setFocusedId(focusNodeId);

    for (const section of view.sections) {
      for (const item of section.items) {
        if (item.page.headline.includes(focusNodeId) || item.page.id.startsWith(focusNodeId)) {
          setExpandedSections((prev) => new Set([...prev, section.name]));
          setExpandedItems((prev) => new Set([...prev, item.page.id]));
          setTimeout(() => {
            document
              .querySelector(`[data-page-id="${item.page.id}"]`)
              ?.scrollIntoView({ behavior: "smooth", block: "center" });
          }, 50);
          break;
        }
      }
    }

    const clearTimer = setTimeout(() => {
      setFocusedId(null);
      onFocusHandled?.();
    }, 3000);
    return () => clearTimeout(clearTimer);
  }, [focusNodeId, view.sections, onFocusHandled]);

  return (
    <div className="vertical-layout">
      <div className="vertical-scroll">
        <header className="vertical-header">
          <h1>{view.question.headline}</h1>
          {view.question.abstract && (
            <div
              className="view-prose"
              style={{
                color: "var(--fg-muted)",
                borderLeft: "2px solid var(--border)",
                paddingLeft: "16px",
              }}
            >
              <p style={{ margin: 0 }}>{view.question.abstract}</p>
            </div>
          )}
          <div className="vertical-date">
            {view.health.total_pages} pages · depth {view.health.max_depth}
          </div>
        </header>

        <div className="vertical-tree">
          {view.sections.map((section, sectionIdx) => {
            const isExpanded = expandedSections.has(section.name);
            return (
              <div key={section.name} className="vertical-node">
                <div className="vertical-node-inner">
                  <div className="vertical-node-header">
                    <span
                      className="vertical-headline"
                      onClick={() => toggleSection(section.name)}
                      style={{ fontWeight: 500 }}
                    >
                      {SECTION_LABELS[section.name] ?? section.name}
                    </span>
                    <button className="vertical-toggle" onClick={() => toggleSection(section.name)}>
                      <span className={`vertical-chevron ${isExpanded ? "open" : ""}`}>
                        {isExpanded ? "\u25BE" : "\u25B8"}
                      </span>
                      <span>
                        {section.items.length}{" "}
                        {section.items.length === 1 ? "item" : "items"}
                      </span>
                    </button>
                  </div>
                </div>

                {isExpanded && (
                  <div
                    className="vertical-children"
                    style={{ borderColor: `var(--active-${sectionIdx % 5})` }}
                  >
                    {section.items.map((item) => (
                      <VerticalItem
                        key={item.page.id}
                        item={item}
                        expanded={expandedItems.has(item.page.id)}
                        onToggle={() => toggleItem(item.page.id)}
                        onFocus={setFocusedId}
                        focusedId={focusedId}
                      />
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
});
