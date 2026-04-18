"use client";

import { useState, useCallback, useRef, useEffect } from "react";
import type { QuestionView, ViewItem, Page } from "@/lib/types";
import { PageCard } from "./PageCard";
import { PageContent } from "./PageContent";

interface PaneState {
  item: ViewItem;
  itemIndex: number;
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

interface StackedPanesProps {
  view: QuestionView;
  focusNodeId?: string | null;
  onFocusHandled?: () => void;
  onOpenSource?: (source: Page) => void;
}

export function StackedPanes({
  view,
  focusNodeId,
  onFocusHandled,
  onOpenSource,
}: StackedPanesProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const lastPaneRef = useRef<HTMLDivElement>(null);

  const [detailPane, setDetailPane] = useState<PaneState | null>(null);
  const [activeItemId, setActiveItemId] = useState<string | null>(null);
  const [focusedId, setFocusedId] = useState<string | null>(null);

  const allItems = view.sections.flatMap((s) => s.items);

  const openDetail = useCallback(
    (item: ViewItem, index: number) => {
      setDetailPane({ item, itemIndex: index });
      setActiveItemId(item.page.id);
    },
    [],
  );

  const closeDetail = useCallback(() => {
    setDetailPane(null);
    setActiveItemId(null);
  }, []);

  useEffect(() => {
    if (!focusNodeId) return;
    setFocusedId(focusNodeId);

    const matchIndex = allItems.findIndex((item) =>
      item.page.headline.includes(focusNodeId) || item.page.id.startsWith(focusNodeId),
    );
    if (matchIndex >= 0) {
      setActiveItemId(allItems[matchIndex].page.id);
      setTimeout(() => {
        const el = document.getElementById(`item-${allItems[matchIndex].page.id}`);
        el?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }, 50);
    }

    const timer = setTimeout(() => {
      setFocusedId(null);
      onFocusHandled?.();
    }, 3000);
    return () => clearTimeout(timer);
  }, [focusNodeId, allItems, onFocusHandled]);

  useEffect(() => {
    if (lastPaneRef.current && detailPane) {
      lastPaneRef.current.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
        inline: "start",
      });
    }
  }, [detailPane]);

  return (
    <div ref={containerRef} className="pane-container">
      {/* Root pane — sections with items */}
      <div className="pane">
        <div style={{ padding: "48px 36px" }}>
          <header style={{ marginBottom: "40px" }}>
            <h1
              style={{
                fontSize: "28px",
                fontWeight: 400,
                lineHeight: 1.3,
                margin: "0 0 20px 0",
                fontFamily: "var(--font-body-stack)",
                letterSpacing: "-0.01em",
              }}
            >
              {view.question.headline}
            </h1>
            {view.question.abstract && (
              <div
                className="view-prose"
                style={{
                  fontSize: "15px",
                  color: "var(--fg-muted)",
                  borderLeft: "2px solid var(--border)",
                  paddingLeft: "16px",
                }}
              >
                <p style={{ margin: 0 }}>{view.question.abstract}</p>
              </div>
            )}
            <div
              style={{
                marginTop: "12px",
                fontFamily: "var(--font-mono-stack)",
                fontSize: "10px",
                color: "var(--fg-dim)",
                letterSpacing: "0.04em",
              }}
            >
              {view.health.total_pages} pages · depth {view.health.max_depth}
            </div>
          </header>

          {view.sections.map((section, sectionIdx) => (
            <div key={section.name} style={{ marginBottom: "32px" }}>
              <div
                style={{
                  fontFamily: "var(--font-mono-stack)",
                  fontSize: "10px",
                  letterSpacing: "0.08em",
                  textTransform: "uppercase",
                  color: "var(--fg-dim)",
                  marginBottom: "4px",
                }}
              >
                {SECTION_LABELS[section.name] ?? section.name}
              </div>
              <div
                style={{
                  fontSize: "12px",
                  color: "var(--fg-dim)",
                  marginBottom: "16px",
                  fontFamily: "var(--font-mono-stack)",
                  letterSpacing: "0.02em",
                }}
              >
                {section.description}
              </div>
              {section.items.map((item, i) => (
                <div key={item.page.id} id={`item-${item.page.id}`}>
                  <PageCard
                    page={item.page}
                    links={item.links}
                    onSelect={() => openDetail(item, i)}
                    onOpenSource={onOpenSource}
                    isActive={activeItemId === item.page.id}
                    isFocused={focusedId ? item.page.headline.includes(focusedId) || item.page.id.startsWith(focusedId) : false}
                    activeDepth={sectionIdx}
                  />
                </div>
              ))}
            </div>
          ))}
        </div>
      </div>

      {/* Detail pane */}
      {detailPane && (
        <div
          ref={lastPaneRef}
          className="pane pane-connected pane-entering"
          style={{ "--active-tint": `var(--active-0)` } as React.CSSProperties}
        >
          <div style={{ padding: "28px 28px 32px" }}>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "baseline",
                marginBottom: "20px",
                paddingBottom: "16px",
                borderBottom: "1px solid var(--border)",
              }}
            >
              <h2
                style={{
                  fontSize: "15px",
                  fontWeight: 500,
                  margin: 0,
                  color: "var(--fg)",
                  lineHeight: 1.4,
                }}
              >
                {detailPane.item.page.headline}
              </h2>
              <button
                onClick={closeDetail}
                style={{
                  background: "none",
                  border: "none",
                  cursor: "pointer",
                  padding: "4px 8px",
                  color: "var(--fg-dim)",
                  fontFamily: "var(--font-mono-stack)",
                  fontSize: "11px",
                  letterSpacing: "0.04em",
                  flexShrink: 0,
                }}
                onMouseEnter={(e) =>
                  (e.currentTarget.style.color = "var(--fg-muted)")
                }
                onMouseLeave={(e) =>
                  (e.currentTarget.style.color = "var(--fg-dim)")
                }
                aria-label="Close pane"
              >
                close
              </button>
            </div>

            <DetailContent item={detailPane.item} />
          </div>
        </div>
      )}
    </div>
  );
}

function DetailContent({
  item,
}: {
  item: ViewItem;
}) {
  const page = item.page;
  const direction = item.links.find(
    (l) => l.link_type === "consideration" && l.direction && l.direction !== "neutral",
  )?.direction;

  return (
    <div>
      <div style={{ display: "flex", gap: "10px", alignItems: "baseline", marginBottom: "16px", flexWrap: "wrap" }}>
        <span
          style={{
            fontFamily: "var(--font-mono-stack)",
            fontSize: "10px",
            letterSpacing: "0.06em",
            textTransform: "uppercase",
            color: "var(--fg-dim)",
          }}
        >
          {page.page_type}
        </span>
        {page.importance !== null && (
          <span style={{ fontFamily: "var(--font-mono-stack)", fontSize: "10px", color: "var(--fg-dim)" }}>
            L{page.importance}
          </span>
        )}
        {direction && (
          <span style={{
            fontFamily: "var(--font-mono-stack)",
            fontSize: "10px",
            color: direction === "supports" ? "var(--link-supports)" : "var(--link-opposes)",
          }}>
            {direction}
          </span>
        )}
        <span style={{ fontFamily: "var(--font-mono-stack)", fontSize: "9px", color: "var(--fg-dim)" }}>
          {page.id.slice(0, 8)}
        </span>
      </div>

      <div style={{ marginBottom: "16px" }}>
        <PageContent text={page.content} excludeConceptId={page.id} />
      </div>

      {page.abstract && page.abstract !== page.content && (
        <div style={{
          padding: "12px 16px",
          background: "var(--bg)",
          borderRadius: "4px",
          marginBottom: "16px",
          fontSize: "13px",
          color: "var(--fg-muted)",
          fontStyle: "italic",
          lineHeight: 1.6,
        }}>
          {page.abstract}
        </div>
      )}

      <CredenceDetail credence={page.credence} robustness={page.robustness} />

      {item.links.length > 0 && (
        <div style={{ marginTop: "16px" }}>
          <div style={{
            fontFamily: "var(--font-mono-stack)",
            fontSize: "10px",
            letterSpacing: "0.06em",
            textTransform: "uppercase",
            color: "var(--fg-dim)",
            marginBottom: "8px",
          }}>
            Links
          </div>
          {item.links.map((link) => (
            <div key={link.id} style={{
              fontSize: "12px",
              color: "var(--fg-muted)",
              padding: "4px 0",
              fontFamily: "var(--font-mono-stack)",
              letterSpacing: "0.02em",
            }}>
              {link.link_type}
              {link.direction && ` (${link.direction})`}
              {" → "}
              {link.to_page_id.slice(0, 8)}
              {link.reasoning && (
                <span style={{ color: "var(--fg-dim)", marginLeft: "8px" }}>
                  {link.reasoning.slice(0, 100)}
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      {page.provenance_call_type && (
        <div style={{
          marginTop: "16px",
          fontFamily: "var(--font-mono-stack)",
          fontSize: "10px",
          color: "var(--fg-dim)",
          letterSpacing: "0.04em",
        }}>
          Created by {page.provenance_call_type}
          {" · "}
          {new Date(page.created_at).toLocaleDateString("en-US", {
            year: "numeric",
            month: "short",
            day: "numeric",
          })}
        </div>
      )}
    </div>
  );
}

function CredenceDetail({ credence, robustness }: { credence: number | null; robustness: number | null }) {
  if (credence === null && robustness === null) return null;

  return (
    <div style={{
      display: "flex",
      gap: "24px",
      padding: "12px 0",
      borderTop: "1px solid var(--border)",
      borderBottom: "1px solid var(--border)",
    }}>
      {credence !== null && (
        <div>
          <div style={{ fontFamily: "var(--font-mono-stack)", fontSize: "10px", color: "var(--fg-dim)", letterSpacing: "0.06em", marginBottom: "4px" }}>
            CREDENCE
          </div>
          <div style={{ fontFamily: "var(--font-mono-stack)", fontSize: "18px", color: "var(--credence-fill)" }}>
            {credence}<span style={{ fontSize: "12px", color: "var(--fg-dim)" }}>/9</span>
          </div>
        </div>
      )}
      {robustness !== null && (
        <div>
          <div style={{ fontFamily: "var(--font-mono-stack)", fontSize: "10px", color: "var(--fg-dim)", letterSpacing: "0.06em", marginBottom: "4px" }}>
            ROBUSTNESS
          </div>
          <div style={{ fontFamily: "var(--font-mono-stack)", fontSize: "18px", color: "var(--robustness-fill)" }}>
            {robustness}<span style={{ fontSize: "12px", color: "var(--fg-dim)" }}>/5</span>
          </div>
        </div>
      )}
    </div>
  );
}
