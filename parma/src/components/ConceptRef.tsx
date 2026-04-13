"use client";

import { useState, useRef, useCallback, useMemo } from "react";
import { useConcepts } from "./ConceptContext";
import type { Page } from "@/lib/types";

function ConceptRefInline({ concept }: { concept: Page }) {
  const [open, setOpen] = useState(false);
  const refEl = useRef<HTMLSpanElement>(null);
  const popoverRef = useRef<HTMLSpanElement>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout>>(undefined);

  const show = useCallback(() => {
    clearTimeout(timeoutRef.current);
    setOpen(true);
  }, []);

  const hide = useCallback(() => {
    timeoutRef.current = setTimeout(() => setOpen(false), 120);
  }, []);

  return (
    <span
      ref={refEl}
      className="concept-ref"
      onMouseEnter={show}
      onMouseLeave={hide}
    >
      {concept.headline}
      {open && (
        <span
          ref={popoverRef}
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

function escapeRegex(str: string): string {
  return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function TextWithConcepts({
  text,
  excludeConceptId,
}: {
  text: string;
  excludeConceptId?: string;
}) {
  const concepts = useConcepts();

  const { regex, conceptMap } = useMemo(() => {
    const filtered = concepts.filter(
      (c) => c.id !== excludeConceptId && c.headline.length > 2,
    );
    if (filtered.length === 0) return { regex: null, conceptMap: new Map<string, Page>() };

    const sorted = [...filtered].sort(
      (a, b) => b.headline.length - a.headline.length,
    );
    const map = new Map<string, Page>();
    const patterns: string[] = [];
    for (const c of sorted) {
      patterns.push(escapeRegex(c.headline));
      map.set(c.headline.toLowerCase(), c);
    }
    const re = new RegExp(`\\b(${patterns.join("|")})\\b`, "gi");
    return { regex: re, conceptMap: map };
  }, [concepts, excludeConceptId]);

  if (!regex) return <>{text}</>;

  const parts: React.ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  const re = new RegExp(regex);
  while ((match = re.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }
    const concept = conceptMap.get(match[1].toLowerCase());
    if (concept) {
      parts.push(<ConceptRefInline key={match.index} concept={concept} />);
    } else {
      parts.push(match[1]);
    }
    lastIndex = re.lastIndex;
  }
  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }
  return <>{parts}</>;
}
