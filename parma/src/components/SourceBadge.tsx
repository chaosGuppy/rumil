"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { fetchSourceByShortId } from "@/lib/api";
import type { SourceFull } from "@/lib/api";

interface SourceBadgeProps {
  sourceIds: string[];
  onOpenDrawer?: (source: SourceFull) => void;
}

function SourcePill({
  shortId,
  onOpenDrawer,
}: {
  shortId: string;
  onOpenDrawer?: (source: SourceFull) => void;
}) {
  const [popoverOpen, setPopoverOpen] = useState(false);
  const [source, setSource] = useState<SourceFull | null>(null);
  const [loading, setLoading] = useState(false);
  const pillRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);

  const handleClick = useCallback(async () => {
    if (popoverOpen) {
      setPopoverOpen(false);
      return;
    }
    setPopoverOpen(true);
    if (!source && !loading) {
      setLoading(true);
      const result = await fetchSourceByShortId(shortId);
      setSource(result);
      setLoading(false);
    }
  }, [popoverOpen, source, loading, shortId]);

  useEffect(() => {
    if (!popoverOpen) return;
    function handleOutside(e: MouseEvent) {
      if (
        pillRef.current &&
        !pillRef.current.contains(e.target as Node) &&
        popoverRef.current &&
        !popoverRef.current.contains(e.target as Node)
      ) {
        setPopoverOpen(false);
      }
    }
    document.addEventListener("mousedown", handleOutside);
    return () => document.removeEventListener("mousedown", handleOutside);
  }, [popoverOpen]);

  return (
    <span style={{ position: "relative", display: "inline-block" }}>
      <button ref={pillRef} className="source-pill" onClick={handleClick}>
        {shortId}
      </button>
      {popoverOpen && (
        <div ref={popoverRef} className="source-popover">
          {loading && (
            <div className="source-popover-loading">Loading...</div>
          )}
          {!loading && !source && (
            <div className="source-popover-empty">
              Source not found
            </div>
          )}
          {!loading && source && (
            <>
              <div className="source-popover-title">{source.title}</div>
              {source.url && (
                <a
                  href={source.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="source-popover-url"
                >
                  {new URL(source.url).hostname}
                  <span className="source-popover-arrow">&thinsp;↗</span>
                </a>
              )}
              {source.abstract && (
                <p className="source-popover-abstract">{source.abstract}</p>
              )}
              {onOpenDrawer && (
                <button
                  className="source-popover-open"
                  onClick={() => {
                    setPopoverOpen(false);
                    onOpenDrawer(source);
                  }}
                >
                  Read full source
                </button>
              )}
            </>
          )}
        </div>
      )}
    </span>
  );
}

export function SourceBadge({ sourceIds, onOpenDrawer }: SourceBadgeProps) {
  if (sourceIds.length === 0) return null;

  return (
    <span className="source-badge-group">
      {sourceIds.map((id) => (
        <SourcePill key={id} shortId={id} onOpenDrawer={onOpenDrawer} />
      ))}
    </span>
  );
}
