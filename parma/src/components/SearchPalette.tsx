"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { searchWorkspace } from "@/lib/api";
import type { Page, SearchResult } from "@/lib/types";

// Keyboard-triggered (Cmd/Ctrl+K) workspace search. One flat result list
// keyed by page id; clicking a row either opens the inspect panel (claims,
// judgements, concepts, etc.) or navigates to the question if the hit is a
// root question. Hits that match on the headline still get a snippet from
// the content column to give the reader something to anchor on.

const DEBOUNCE_MS = 150;

export interface SearchPaletteProps {
  projectId: string;
  open: boolean;
  onClose: () => void;
  onOpenPage: (page: Page) => void;
  // Navigate to a question (which is a first-class route, not an inspect
  // panel peek). The parent decides how — usually by setting the ?q= param.
  onOpenQuestion: (page: Page) => void;
}

export function SearchPalette({
  projectId,
  open,
  onClose,
  onOpenPage,
  onOpenQuestion,
}: SearchPaletteProps) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedIdx, setSelectedIdx] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Focus on open, reset state on close so the next open starts clean.
  useEffect(() => {
    if (open) {
      inputRef.current?.focus();
    } else {
      setQuery("");
      setResults([]);
      setSelectedIdx(0);
      setError(null);
    }
  }, [open]);

  // Debounced search. Abort the previous fetch if the user types fast so
  // late responses don't clobber earlier ones.
  useEffect(() => {
    if (!open) return;
    const trimmed = query.trim();
    if (!trimmed) {
      setResults([]);
      setLoading(false);
      setError(null);
      return;
    }
    setLoading(true);
    let cancelled = false;
    const handle = window.setTimeout(() => {
      searchWorkspace(projectId, trimmed, 30)
        .then((hits) => {
          if (cancelled) return;
          setResults(hits);
          setSelectedIdx(0);
          setLoading(false);
          setError(null);
        })
        .catch((e) => {
          if (cancelled) return;
          setError(e instanceof Error ? e.message : "search failed");
          setLoading(false);
        });
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, [open, projectId, query]);

  const openHit = useCallback(
    (hit: SearchResult) => {
      if (hit.page.page_type === "question") {
        onOpenQuestion(hit.page);
      } else {
        onOpenPage(hit.page);
      }
      onClose();
    },
    [onOpenPage, onOpenQuestion, onClose],
  );

  // Arrow keys + Enter, plus Esc to dismiss. Navigation wraps at both ends
  // so the user never has to hunt for the edge.
  const handleKey = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }
      if (!results.length) return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedIdx((i) => (i + 1) % results.length);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedIdx((i) => (i - 1 + results.length) % results.length);
      } else if (e.key === "Enter") {
        e.preventDefault();
        const hit = results[selectedIdx];
        if (hit) openHit(hit);
      }
    },
    [results, selectedIdx, openHit, onClose],
  );

  // Scroll the selected row into view on arrow navigation.
  useEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const row = list.querySelector<HTMLElement>(
      `[data-search-idx="${selectedIdx}"]`,
    );
    row?.scrollIntoView({ block: "nearest" });
  }, [selectedIdx]);

  const highlighted = useMemo(() => query.trim(), [query]);

  if (!open) return null;

  return (
    <div
      className="search-palette-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="search-palette"
        role="dialog"
        aria-label="Workspace search"
      >
        <div className="search-palette-input-row">
          <span className="search-palette-kbd">⌘K</span>
          <input
            ref={inputRef}
            className="search-palette-input"
            type="text"
            value={query}
            placeholder="search this workspace — pages, claims, questions..."
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKey}
            autoComplete="off"
            spellCheck={false}
          />
          {loading && <span className="search-palette-loading">...</span>}
        </div>
        {error && <div className="search-palette-error">{error}</div>}
        {!error && query.trim() && !loading && results.length === 0 && (
          <div className="search-palette-empty">no matches</div>
        )}
        <div className="search-palette-results" ref={listRef}>
          {results.map((hit, i) => (
            <SearchResultRow
              key={hit.page.id}
              hit={hit}
              selected={i === selectedIdx}
              query={highlighted}
              idx={i}
              onClick={() => openHit(hit)}
              onHover={() => setSelectedIdx(i)}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function SearchResultRow({
  hit,
  selected,
  query,
  idx,
  onClick,
  onHover,
}: {
  hit: SearchResult;
  selected: boolean;
  query: string;
  idx: number;
  onClick: () => void;
  onHover: () => void;
}) {
  return (
    <button
      className={`search-palette-row ${selected ? "is-selected" : ""}`}
      data-search-idx={idx}
      onClick={onClick}
      onMouseMove={onHover}
    >
      <div className="search-palette-row-head">
        <span
          className={`search-palette-type search-palette-type-${hit.page.page_type}`}
        >
          {hit.page.page_type}
        </span>
        <span className="search-palette-headline">
          {highlight(hit.page.headline, query)}
        </span>
        <span className="search-palette-shortid">
          {hit.page.id.slice(0, 8)}
        </span>
      </div>
      {hit.snippet && (
        <div className="search-palette-snippet">
          {highlight(hit.snippet, query)}
        </div>
      )}
    </button>
  );
}

// Minimal case-insensitive highlighter — splits the string around each
// occurrence of the query and wraps hits in a <mark>. Kept inline so the
// palette has no extra dependencies.
function highlight(text: string, query: string): React.ReactNode {
  if (!query) return text;
  const lower = text.toLowerCase();
  const needle = query.toLowerCase();
  const parts: React.ReactNode[] = [];
  let i = 0;
  let key = 0;
  while (i < text.length) {
    const idx = lower.indexOf(needle, i);
    if (idx < 0) {
      parts.push(text.slice(i));
      break;
    }
    if (idx > i) parts.push(text.slice(i, idx));
    parts.push(
      <mark key={key++} className="search-palette-mark">
        {text.slice(idx, idx + needle.length)}
      </mark>,
    );
    i = idx + needle.length;
  }
  return <>{parts}</>;
}

// Global keyboard hook — returns [open, setOpen]. Listens for Cmd/Ctrl+K
// and toggles the palette. Hosts it at the page level so any view can mount
// the palette without having to re-wire the shortcut.
export function useSearchPaletteShortcut(): [
  boolean,
  React.Dispatch<React.SetStateAction<boolean>>,
] {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        setOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return [open, setOpen];
}
