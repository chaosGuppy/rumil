"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import type { QuestionView, ViewItem, Page } from "@/lib/types";
import { CredenceBadge } from "./CredenceBadge";
import { LinkBadges } from "./LinkBadges";
import { NodeTypeLabel } from "./NodeTypeLabel";
import { SourceBadge } from "./SourceBadge";
import { PageContent } from "./PageContent";
import { PageAnnotationActions } from "./PageAnnotationActions";

interface ArticleViewProps {
  view: QuestionView;
  focusNodeId?: string | null;
  onFocusHandled?: () => void;
  onOpenSource?: (source: Page) => void;
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

function directionLabel(item: ViewItem): string | null {
  for (const link of item.links) {
    if (link.link_type === "consideration" && link.direction && link.direction !== "neutral") {
      return link.direction;
    }
  }
  return null;
}

function ArticleItem({
  item,
  onFocus,
  focusedId,
  onOpenSource,
}: {
  item: ViewItem;
  onFocus: (headline: string) => void;
  focusedId: string | null;
  onOpenSource?: (source: Page) => void;
}) {
  const page = item.page;
  const isFocused = focusedId ? page.headline.includes(focusedId) : false;
  const direction = directionLabel(item);
  const citedSourceIds = item.links
    .filter((l) => l.link_type === "cites")
    .map((l) => l.to_page_id.slice(0, 8));

  return (
    <div
      className={[
        "article-node",
        "article-depth-1",
        isFocused ? "node-focused" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      data-headline={page.headline}
    >
      <h3 className="article-heading" onClick={() => onFocus(page.headline)}>
        {page.headline}
      </h3>
      <div className="article-meta">
        <NodeTypeLabel type={page.page_type} />
        {page.importance !== null && page.importance > 0 && (
          <span className="article-label-dim">L{page.importance}</span>
        )}
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
        <SourceBadge sourceIds={citedSourceIds} onOpenDrawer={onOpenSource} />
        <LinkBadges links={item.links} />
        <PageAnnotationActions pageId={page.id} />
      </div>
      <PageContent
        text={page.content}
        excludeConceptId={page.id}
        pageId={page.id}
      />
    </div>
  );
}

export function ArticleView({
  view,
  focusNodeId,
  onFocusHandled,
  onOpenSource,
}: ArticleViewProps) {
  const [activeSection, setActiveSection] = useState(0);
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!focusNodeId) return;
    setFocusedId(focusNodeId);
    const scrollTimer = setTimeout(() => {
      const el = scrollRef.current?.querySelector(
        `[data-headline*="${CSS.escape(focusNodeId)}"]`,
      );
      el?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 50);
    const clearTimer = setTimeout(() => {
      setFocusedId(null);
      onFocusHandled?.();
    }, 3000);
    return () => {
      clearTimeout(scrollTimer);
      clearTimeout(clearTimer);
    };
  }, [focusNodeId, onFocusHandled]);

  useEffect(() => {
    const root = scrollRef.current;
    if (!root) return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            const idx = Number(entry.target.getAttribute("data-section"));
            if (!isNaN(idx)) setActiveSection(idx);
          }
        }
      },
      { root, threshold: 0.15 },
    );
    root
      .querySelectorAll("[data-section]")
      .forEach((el) => observer.observe(el));
    return () => observer.disconnect();
  }, [view]);

  const scrollToSection = useCallback((index: number) => {
    document
      .getElementById(`section-${index}`)
      ?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, []);

  return (
    <div className="article-layout">
      <nav className="article-toc">
        <div className="article-toc-title">Sections</div>
        {view.sections.map((section, i) => (
          <button
            key={section.name}
            className={`article-toc-item ${activeSection === i ? "active" : ""}`}
            onClick={() => scrollToSection(i)}
          >
            <span
              className="article-toc-dot"
              style={{ background: "var(--accent)" }}
            />
            <span>
              {SECTION_LABELS[section.name] ?? section.name}
              <span style={{
                fontFamily: "var(--font-mono-stack)",
                fontSize: "9px",
                color: "var(--fg-dim)",
                marginLeft: "6px",
              }}>
                {section.items.length}
              </span>
            </span>
          </button>
        ))}
      </nav>

      <div className="article-scroll" ref={scrollRef}>
        <article className="article-content">
          <header className="article-header">
            <h1>{view.question.headline}</h1>
            {view.question.abstract && (
              <div className="article-summary view-prose">
                <p>{view.question.abstract}</p>
              </div>
            )}
            <div className="article-date">
              {view.health.total_pages} pages · depth {view.health.max_depth}
              {view.health.missing_credence > 0 && ` · ${view.health.missing_credence} missing credence`}
            </div>
          </header>

          {view.sections.map((section, i) => (
            <section
              key={section.name}
              id={`section-${i}`}
              className="article-section"
              data-section={i}
            >
              {i > 0 && <hr className="article-divider" />}
              <h2 className="article-heading" style={{ fontSize: "22px", marginBottom: "4px" }}>
                {SECTION_LABELS[section.name] ?? section.name}
              </h2>
              <p style={{
                fontSize: "13px",
                color: "var(--fg-dim)",
                margin: "0 0 20px 0",
                fontFamily: "var(--font-mono-stack)",
                letterSpacing: "0.02em",
              }}>
                {section.description}
              </p>
              {section.items.map((item) => (
                <ArticleItem
                  key={item.page.id}
                  item={item}
                  onFocus={setFocusedId}
                  focusedId={focusedId}
                  onOpenSource={onOpenSource}
                />
              ))}
            </section>
          ))}
        </article>
      </div>
    </div>
  );
}
