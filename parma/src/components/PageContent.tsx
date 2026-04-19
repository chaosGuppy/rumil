"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { NODE_ID_RE, isPromoteEvent } from "./NodeRefLink";
import { useConcepts } from "./ConceptContext";
import { useInspectPanel } from "./InspectPanelContext";
import {
  useAnnotations,
  useRegisterPage,
} from "./AnnotationContext";
import type { AnnotationEvent } from "@/lib/annotations";
import type { Page } from "@/lib/types";
import { AnnotationToolbar } from "./AnnotationToolbar";
import type { ToolbarSelection } from "./AnnotationToolbar";
import { AnnotationDrawer } from "./AnnotationDrawer";

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
 *   4. When `pageId` is provided: attach annotation affordances —
 *      - selection → floating toolbar
 *      - existing span annotations overlaid as underlines
 *      - page-level annotation indicator + drawer toggle
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
 *
 * Span anchoring: offsets are into the rendered `textContent` of the body,
 * not the markdown source. We picked textContent-offsets because they're
 * easy to round-trip between selection.toString() and mark overlay, and
 * because concept/ref linkification doesn't alter the character stream.
 * Drift risk is real — if the page is superseded with new text, offsets
 * will silently misalign. Acceptable for the MVP per doc 28; a v2 would
 * anchor via prefix+suffix text match.
 */

interface PageContentProps {
  text: string;
  onNodeRef?: (id: string) => void;
  excludeConceptId?: string;
  className?: string;
  /** Override the default inline-prose tag; useful when embedding in a list. */
  inline?: boolean;
  /**
   * When set, this PageContent becomes annotatable: text selections raise a
   * floating toolbar, existing spans are underlined, and a page-level
   * indicator opens an annotations drawer. Leave undefined for contexts
   * where annotation makes no sense (e.g. concept popovers, toy renders).
   */
  pageId?: string;
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
  onPromote: ((id: string) => void) | undefined,
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
          onMouseDown={(e) => {
            if (onPromote && isPromoteEvent(e)) e.preventDefault();
          }}
          onClick={(e) => {
            if (onPromote && isPromoteEvent(e)) {
              e.preventDefault();
              onPromote(matched);
            } else {
              onNodeRef(matched);
            }
          }}
          className="node-ref-link"
          title={`Click to inspect · alt/cmd-click to pin as pane · ${matched}`}
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
  pageId,
}: PageContentProps) {
  const inspect = useInspectPanel();
  const handleNodeRef = onNodeRef ?? inspect.openInspect;
  const handlePromote = inspect.promoteToPane;
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
            handlePromote,
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
                handlePromote,
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
    [handleNodeRef, handlePromote, conceptRegex, conceptMap],
  );

  const wrapperClass = className ?? "view-prose";

  const markdown = (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={
        inline
          ? {
              p: ({ children }) => <>{processChildren(children, "p")}</>,
              li: ({ children }) => (
                <li>{processChildren(children, "li")}</li>
              ),
              strong: ({ children }) => (
                <strong>{processChildren(children, "strong")}</strong>
              ),
              em: ({ children }) => (
                <em>{processChildren(children, "em")}</em>
              ),
              a: ({ children, href }) => (
                <a href={href} target="_blank" rel="noopener noreferrer">
                  {processChildren(children, "a")}
                </a>
              ),
            }
          : {
              p: ({ children }) => <p>{processChildren(children, "p")}</p>,
              li: ({ children }) => (
                <li>{processChildren(children, "li")}</li>
              ),
              strong: ({ children }) => (
                <strong>{processChildren(children, "strong")}</strong>
              ),
              em: ({ children }) => (
                <em>{processChildren(children, "em")}</em>
              ),
              h1: ({ children }) => (
                <h1>{processChildren(children, "h1")}</h1>
              ),
              h2: ({ children }) => (
                <h2>{processChildren(children, "h2")}</h2>
              ),
              h3: ({ children }) => (
                <h3>{processChildren(children, "h3")}</h3>
              ),
              h4: ({ children }) => (
                <h4>{processChildren(children, "h4")}</h4>
              ),
              blockquote: ({ children }) => (
                <blockquote>{processChildren(children, "bq")}</blockquote>
              ),
              a: ({ children, href }) => (
                <a href={href} target="_blank" rel="noopener noreferrer">
                  {processChildren(children, "a")}
                </a>
              ),
            }
      }
    >
      {text}
    </ReactMarkdown>
  );

  // Non-annotatable path: same render tree the file has always produced.
  if (!pageId) {
    if (inline) return <span className={wrapperClass}>{markdown}</span>;
    return <div className={wrapperClass}>{markdown}</div>;
  }

  return (
    <AnnotatablePageContent
      pageId={pageId}
      wrapperClass={wrapperClass}
      inline={inline}
    >
      {markdown}
    </AnnotatablePageContent>
  );
}

// Wrapper that handles the annotation-specific side of the world. Kept as
// a sibling component so the non-annotatable path stays cheap and the hook
// surface is isolated from the ReactMarkdown config above.
function AnnotatablePageContent({
  pageId,
  wrapperClass,
  inline,
  children,
}: {
  pageId: string;
  wrapperClass: string;
  inline: boolean;
  children: ReactNode;
}) {
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const spanBodyRef = useRef<HTMLSpanElement | null>(null);
  const [selection, setSelection] = useState<ToolbarSelection | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const { annotationsForPage } = useAnnotations();
  useRegisterPage(pageId);

  const annotations = annotationsForPage(pageId);
  const spanAnnotations = useMemo(
    () =>
      annotations.filter(
        (a) => a.span_start !== null && a.span_end !== null,
      ),
    [annotations],
  );
  const pageLevelCount = annotations.length - spanAnnotations.length;

  const handleSelection = useCallback(() => {
    const sel = window.getSelection();
    const body = bodyRef.current ?? spanBodyRef.current;
    if (!sel || !body || sel.isCollapsed) {
      setSelection(null);
      return;
    }
    if (!body.contains(sel.anchorNode) || !body.contains(sel.focusNode)) {
      setSelection(null);
      return;
    }
    const range = sel.getRangeAt(0);
    const text = sel.toString();
    if (!text.trim()) {
      setSelection(null);
      return;
    }
    const [start, end] = offsetsForRange(body, range);
    if (start === end) {
      setSelection(null);
      return;
    }
    const rect = range.getBoundingClientRect();
    setSelection({ pageId, text, start, end, anchorRect: rect });
  }, [pageId]);

  // Overlay existing span annotations as <mark> underlines. Done
  // imperatively after render because we don't own the inner DOM (it comes
  // from react-markdown). This is a best-effort: if a span crosses block
  // boundaries, we underline the portion that fits in a single text node
  // range and let Range.surroundContents refuse otherwise (we catch).
  useEffect(() => {
    const body = bodyRef.current ?? spanBodyRef.current;
    if (!body) return;
    // Remove any prior marks.
    body.querySelectorAll("mark.ann-mark").forEach((m) => {
      const parent = m.parentNode;
      if (!parent) return;
      while (m.firstChild) parent.insertBefore(m.firstChild, m);
      parent.removeChild(m);
      parent.normalize?.();
    });
    if (spanAnnotations.length === 0) return;

    // Group by (start,end) so duplicate spans show one underline with a
    // count rather than stacking.
    const grouped = new Map<string, AnnotationEvent[]>();
    for (const a of spanAnnotations) {
      const key = `${a.span_start}:${a.span_end}`;
      const bucket = grouped.get(key) ?? [];
      bucket.push(a);
      grouped.set(key, bucket);
    }

    for (const [key, anns] of grouped) {
      const [s, e] = key.split(":").map(Number);
      const range = rangeForOffsets(body, s, e);
      if (!range) continue;
      const mark = document.createElement("mark");
      mark.className = `ann-mark ann-mark-${kindClass(anns)}`;
      mark.dataset.annKey = key;
      mark.title = annotationTooltip(anns);
      try {
        range.surroundContents(mark);
        if (anns.length > 1) {
          const sup = document.createElement("sup");
          sup.className = "ann-mark-count";
          sup.textContent = `(${anns.length})`;
          mark.appendChild(sup);
        }
      } catch {
        // Range spans multiple block elements; skip. A future patch could
        // split the range across text nodes instead.
      }
    }
  }, [spanAnnotations]);

  const indicator =
    pageLevelCount > 0 ? (
      <button
        type="button"
        className="ann-page-indicator"
        onClick={() => setDrawerOpen(true)}
      >
        <span aria-hidden>📎</span>
        {pageLevelCount} {pageLevelCount === 1 ? "annotation" : "annotations"}{" "}
        on this page
      </button>
    ) : spanAnnotations.length > 0 ? (
      <button
        type="button"
        className="ann-page-indicator ann-page-indicator-dim"
        onClick={() => setDrawerOpen(true)}
      >
        <span aria-hidden>📎</span>
        {spanAnnotations.length} span{" "}
        {spanAnnotations.length === 1 ? "annotation" : "annotations"}
      </button>
    ) : null;

  if (inline) {
    return (
      <span
        className={wrapperClass}
        onMouseUp={handleSelection}
        onTouchEnd={handleSelection}
      >
        <span ref={spanBodyRef} className="ann-body">
          {children}
        </span>
        {indicator}
        <AnnotationToolbar
          selection={selection}
          onClose={() => setSelection(null)}
        />
        {drawerOpen && (
          <AnnotationDrawer
            pageId={pageId}
            pageText={bodyText(spanBodyRef.current)}
            annotations={annotations}
            onClose={() => setDrawerOpen(false)}
          />
        )}
      </span>
    );
  }

  return (
    <div
      className={wrapperClass}
      onMouseUp={handleSelection}
      onTouchEnd={handleSelection}
    >
      {indicator}
      <div ref={bodyRef} className="ann-body">
        {children}
      </div>
      <AnnotationToolbar
        selection={selection}
        onClose={() => setSelection(null)}
      />
      {drawerOpen && (
        <AnnotationDrawer
          pageId={pageId}
          pageText={bodyText(bodyRef.current)}
          annotations={annotations}
          onClose={() => setDrawerOpen(false)}
        />
      )}
    </div>
  );
}

// Return [startOffset, endOffset] into the concatenated textContent of
// `root`, given a Range inside it.
function offsetsForRange(root: HTMLElement, range: Range): [number, number] {
  const start = charOffsetToNode(root, range.startContainer, range.startOffset);
  const end = charOffsetToNode(root, range.endContainer, range.endOffset);
  return start <= end ? [start, end] : [end, start];
}

function charOffsetToNode(
  root: HTMLElement,
  target: Node,
  offsetInTarget: number,
): number {
  let offset = 0;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node: Node | null = walker.nextNode();
  while (node) {
    if (node === target) return offset + offsetInTarget;
    offset += (node.nodeValue ?? "").length;
    node = walker.nextNode();
  }
  // target wasn't a text node (e.g. the element itself) — fall back to the
  // offset of the element within root's text stream, treating offsetInTarget
  // as a child-node index into its text children.
  if (target instanceof HTMLElement && root.contains(target)) {
    let acc = 0;
    const w2 = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n: Node | null = w2.nextNode();
    while (n) {
      if (target.contains(n)) return acc;
      acc += (n.nodeValue ?? "").length;
      n = w2.nextNode();
    }
    return acc;
  }
  return offset;
}

// Inverse of offsetsForRange: build a Range spanning [start, end] within
// root's textContent. Returns null if out-of-bounds.
function rangeForOffsets(
  root: HTMLElement,
  start: number,
  end: number,
): Range | null {
  if (start < 0 || end <= start) return null;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let acc = 0;
  let startNode: Node | null = null;
  let startOff = 0;
  let endNode: Node | null = null;
  let endOff = 0;
  let node: Node | null = walker.nextNode();
  while (node) {
    const len = (node.nodeValue ?? "").length;
    if (!startNode && acc + len >= start) {
      startNode = node;
      startOff = start - acc;
    }
    if (acc + len >= end) {
      endNode = node;
      endOff = end - acc;
      break;
    }
    acc += len;
    node = walker.nextNode();
  }
  if (!startNode || !endNode) return null;
  try {
    const range = document.createRange();
    range.setStart(startNode, startOff);
    range.setEnd(endNode, endOff);
    return range;
  } catch {
    return null;
  }
}

function bodyText(root: HTMLElement | null): string {
  return root?.textContent ?? "";
}

function kindClass(anns: AnnotationEvent[]): string {
  for (const a of anns) {
    if (a.category === "dispute" || a.category === "factual_error") {
      return "dispute";
    }
  }
  for (const a of anns) {
    if (a.category === "endorsement") return "endorse";
  }
  return "comment";
}

function annotationTooltip(anns: AnnotationEvent[]): string {
  return anns
    .map((a) => {
      const kind = a.category ?? a.annotation_type;
      const note = a.note ? ` — ${a.note}` : "";
      return `${kind} (${a.author_type})${note}`;
    })
    .join("\n");
}
