"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { NODE_ID_RE } from "./NodeRefLink";
import { useConcepts } from "./ConceptContext";
import { useInspectPanel } from "./InspectPanelContext";
import type { Page } from "@/lib/types";

/**
 * PageContent — the canonical renderer for `page.content` strings.
 *
 * Responsibilities, in a single pass:
 *   1. Render markdown (headings, bold, lists, tables) via react-markdown
 *      with the same `remarkGfm` setup ChatPanel already uses.
 *   2. Linkify 8-char hex node refs (`[abc12345]` or bare `abc12345`) into
 *      clickable spans that call `onNodeRef(id)` — defaulting to the
 *      global inspect panel.
 *   3. Linkify inline concept matches (headlines of `concept` pages), the
 *      same behavior `TextWithConcepts` previously provided.
 *
 * Option B (unified pass) rationale: scanning each markdown text leaf once
 * for both node refs and concepts, rather than nesting a second pass on
 * top of rendered markdown, avoids two problems:
 *   (a) Concepts match by headline *within* a text run — the old
 *       `TextWithConcepts` did substring matching. Applying it on top of
 *       already-sliced node-ref parts risks breaking matches that straddle
 *       a ref. Scanning once with a combined pattern matches left-to-right
 *       with a single lastIndex.
 *   (b) It keeps the React tree shallow: every text leaf is replaced by a
 *       flat array of strings/buttons/spans, no extra wrapper divs.
 *
 * Both refs and concepts are optional: if no concepts are loaded and no
 * onNodeRef is provided, the function fast-paths back to raw strings.
 */

interface PageContentProps {
  text: string;
  onNodeRef?: (id: string) => void;
  excludeConceptId?: string;
  className?: string;
  /** Override the default inline-prose tag; useful when embedding in a list. */
  inline?: boolean;
}

function escapeRegex(str: string): string {
  return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Inline ConceptRef popover — colocated since it's only used by the
// unified text processor below. Extracting it isn't worth the indirection
// given it's pure presentation.
function ConceptRefInline({ concept }: { concept: Page }) {
  const [open, setOpen] = useState(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout>>(undefined);

  const show = useCallback(() => {
    clearTimeout(timeoutRef.current);
    setOpen(true);
  }, []);
  const hide = useCallback(() => {
    timeoutRef.current = setTimeout(() => setOpen(false), 120);
  }, []);

  return (
    <span className="concept-ref" onMouseEnter={show} onMouseLeave={hide}>
      {concept.headline}
      {open && (
        <span
          className="concept-popover"
          onMouseEnter={show}
          onMouseLeave={hide}
        >
          <span className="concept-popover-label">concept</span>
          <span className="concept-popover-headline">{concept.headline}</span>
          <span className="concept-popover-content">{concept.content}</span>
        </span>
      )}
    </span>
  );
}

// Split a raw string into a mixed array of strings, node-ref buttons, and
// concept-ref spans, scanning once from left to right.
function linkifyString(
  text: string,
  onNodeRef: ((id: string) => void) | undefined,
  conceptRegex: RegExp | null,
  conceptMap: Map<string, Page>,
  keyPrefix: string,
): ReactNode[] {
  // Build a combined regex: node-refs OR concept headlines. If either side
  // is absent we just use the other, which keeps the common case (no
  // concepts loaded yet) identical to the pure-node-ref scan ChatPanel had.
  const patterns: string[] = [];
  if (onNodeRef) patterns.push(NODE_ID_RE.source);
  if (conceptRegex) patterns.push(conceptRegex.source);
  if (patterns.length === 0) return [text];

  const combined = new RegExp(patterns.join("|"), "gi");
  const parts: ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = combined.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }
    const matched = match[0];

    // Node ref — 8 lowercase hex with word boundaries
    if (onNodeRef && /^[0-9a-f]{8}$/.test(matched)) {
      parts.push(
        <button
          key={`${keyPrefix}-ref-${match.index}`}
          type="button"
          onClick={() => onNodeRef(matched)}
          className="node-ref-link"
          title={`Inspect ${matched}`}
        >
          {matched}
        </button>,
      );
    } else {
      // Concept headline (case-insensitive)
      const concept = conceptMap.get(matched.toLowerCase());
      if (concept) {
        parts.push(
          <ConceptRefInline
            key={`${keyPrefix}-concept-${match.index}`}
            concept={concept}
          />,
        );
      } else {
        parts.push(matched);
      }
    }
    lastIndex = combined.lastIndex;
  }
  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }
  return parts;
}

export function PageContent({
  text,
  onNodeRef,
  excludeConceptId,
  className,
  inline = false,
}: PageContentProps) {
  const inspect = useInspectPanel();
  const handleNodeRef = onNodeRef ?? inspect.openInspect;
  const concepts = useConcepts();

  const { conceptRegex, conceptMap } = useMemo(() => {
    const filtered = concepts.filter(
      (c) => c.id !== excludeConceptId && c.headline.length > 2,
    );
    if (filtered.length === 0) {
      return { conceptRegex: null, conceptMap: new Map<string, Page>() };
    }
    // Longest headlines first so "self-driving car" beats "car".
    const sorted = [...filtered].sort(
      (a, b) => b.headline.length - a.headline.length,
    );
    const map = new Map<string, Page>();
    const patterns: string[] = [];
    for (const c of sorted) {
      patterns.push(escapeRegex(c.headline));
      map.set(c.headline.toLowerCase(), c);
    }
    return {
      conceptRegex: new RegExp(`\\b(?:${patterns.join("|")})\\b`, "gi"),
      conceptMap: map,
    };
  }, [concepts, excludeConceptId]);

  const processChildren = useCallback(
    (children: ReactNode, keyPrefix: string): ReactNode => {
      if (!handleNodeRef && !conceptRegex) return children;
      if (!Array.isArray(children)) {
        if (typeof children === "string") {
          return linkifyString(
            children,
            handleNodeRef,
            conceptRegex,
            conceptMap,
            keyPrefix,
          );
        }
        return children;
      }
      return children.map((child, i) => {
        if (typeof child === "string") {
          return (
            <span key={`${keyPrefix}-${i}`}>
              {linkifyString(
                child,
                handleNodeRef,
                conceptRegex,
                conceptMap,
                `${keyPrefix}-${i}`,
              )}
            </span>
          );
        }
        return child;
      });
    },
    [handleNodeRef, conceptRegex, conceptMap],
  );

  const wrapperClass = className ?? "view-prose";

  // For inline use (e.g. inside an existing <p>), render a span so we
  // don't nest block elements.
  if (inline) {
    return (
      <span className={wrapperClass}>
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            // Unwrap paragraphs when asked for inline rendering.
            p: ({ children }) => <>{processChildren(children, "p")}</>,
            li: ({ children }) => <li>{processChildren(children, "li")}</li>,
            strong: ({ children }) => (
              <strong>{processChildren(children, "strong")}</strong>
            ),
            em: ({ children }) => <em>{processChildren(children, "em")}</em>,
            a: ({ children, href }) => (
              <a href={href} target="_blank" rel="noopener noreferrer">
                {processChildren(children, "a")}
              </a>
            ),
          }}
        >
          {text}
        </ReactMarkdown>
      </span>
    );
  }

  return (
    <div className={wrapperClass}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          p: ({ children }) => <p>{processChildren(children, "p")}</p>,
          li: ({ children }) => <li>{processChildren(children, "li")}</li>,
          strong: ({ children }) => (
            <strong>{processChildren(children, "strong")}</strong>
          ),
          em: ({ children }) => <em>{processChildren(children, "em")}</em>,
          h1: ({ children }) => <h1>{processChildren(children, "h1")}</h1>,
          h2: ({ children }) => <h2>{processChildren(children, "h2")}</h2>,
          h3: ({ children }) => <h3>{processChildren(children, "h3")}</h3>,
          h4: ({ children }) => <h4>{processChildren(children, "h4")}</h4>,
          blockquote: ({ children }) => (
            <blockquote>{processChildren(children, "bq")}</blockquote>
          ),
          a: ({ children, href }) => (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {processChildren(children, "a")}
            </a>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
