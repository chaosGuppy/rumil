"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import type { WorldviewNode, Worldview } from "@/lib/types";
import { CredenceBadge } from "./CredenceBadge";
import { NodeTypeLabel, nodeColor } from "./NodeTypeLabel";

interface ArticleViewProps {
  worldview: Worldview;
  focusNodeId?: string | null;
  onFocusHandled?: () => void;
}

function isSupplementary(node: WorldviewNode): boolean {
  return node.importance !== undefined && node.importance >= 3;
}

function collectSupplementary(node: WorldviewNode): WorldviewNode[] {
  const result: WorldviewNode[] = [];
  for (const child of node.children) {
    if (isSupplementary(child)) {
      result.push(child);
    } else {
      result.push(...collectSupplementary(child));
    }
  }
  return result;
}

function ArticleNode({
  node,
  depth,
  onFocus,
  focusedId,
}: {
  node: WorldviewNode;
  depth: number;
  onFocus: (headline: string) => void;
  focusedId: string | null;
}) {
  if (isSupplementary(node)) return null;

  const isFocused = focusedId ? node.headline.includes(focusedId) : false;
  const regular = node.children.filter((c) => !isSupplementary(c));
  const Tag = (
    depth === 0 ? "h2" : depth === 1 ? "h3" : "h4"
  ) as "h2" | "h3" | "h4";

  return (
    <div
      className={[
        "article-node",
        `article-depth-${Math.min(depth, 2)}`,
        isFocused ? "node-focused" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      data-headline={node.headline}
    >
      <Tag className="article-heading" onClick={() => onFocus(node.headline)}>
        {node.headline}
      </Tag>
      <div className="article-meta">
        <NodeTypeLabel type={node.node_type} />
        {node.importance !== undefined && node.importance > 0 && (
          <span className="article-label-dim">L{node.importance}</span>
        )}
        <CredenceBadge credence={node.credence} robustness={node.robustness} />
        {node.source_page_ids.length > 0 && (
          <span className="article-label-dim">
            {node.source_page_ids.length} src
          </span>
        )}
      </div>
      <div className="worldview-prose">
        <p>{node.content}</p>
      </div>
      {regular.map((child, i) => (
        <ArticleNode
          key={i}
          node={child}
          depth={depth + 1}
          onFocus={onFocus}
          focusedId={focusedId}
        />
      ))}
    </div>
  );
}

export function ArticleView({
  worldview,
  focusNodeId,
  onFocusHandled,
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
  }, [worldview]);

  const scrollToSection = useCallback((index: number) => {
    document
      .getElementById(`section-${index}`)
      ?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, []);

  return (
    <div className="article-layout">
      <nav className="article-toc">
        <div className="article-toc-title">Contents</div>
        {worldview.nodes.map((node, i) => (
          <button
            key={i}
            className={`article-toc-item ${activeSection === i ? "active" : ""}`}
            onClick={() => scrollToSection(i)}
          >
            <span
              className="article-toc-dot"
              style={{ background: nodeColor(node.node_type) }}
            />
            <span>{node.headline}</span>
          </button>
        ))}
      </nav>

      <div className="article-scroll" ref={scrollRef}>
        <article className="article-content">
          <header className="article-header">
            <h1>{worldview.question_headline}</h1>
            <div className="article-summary worldview-prose">
              <p>{worldview.summary}</p>
            </div>
            <div className="article-date">
              Generated{" "}
              {new Date(worldview.generated_at).toLocaleDateString("en-US", {
                year: "numeric",
                month: "long",
                day: "numeric",
              })}
            </div>
          </header>

          {worldview.nodes.map((node, i) => {
            const supplementary = collectSupplementary(node);
            return (
              <section
                key={i}
                id={`section-${i}`}
                className="article-section"
                data-section={i}
              >
                {i > 0 && <hr className="article-divider" />}
                <ArticleNode
                  node={node}
                  depth={0}
                  onFocus={setFocusedId}
                  focusedId={focusedId}
                />
                {supplementary.length > 0 && (
                  <details className="article-supplementary">
                    <summary className="article-supplementary-toggle">
                      Supplementary
                      <span className="article-supplementary-count">
                        {supplementary.length}
                      </span>
                    </summary>
                    <div className="article-supplementary-body">
                      {supplementary.map((sNode, j) => (
                        <div key={j} className="article-supplementary-item">
                          <div className="article-meta">
                            <NodeTypeLabel type={sNode.node_type} />
                            <CredenceBadge
                              credence={sNode.credence}
                              robustness={sNode.robustness}
                            />
                          </div>
                          <h4 className="article-supplementary-heading">
                            {sNode.headline}
                          </h4>
                          <div className="worldview-prose">
                            <p>{sNode.content}</p>
                          </div>
                        </div>
                      ))}
                    </div>
                  </details>
                )}
              </section>
            );
          })}
        </article>
      </div>
    </div>
  );
}
