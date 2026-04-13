"use client";

import { useEffect, useRef } from "react";
import type { Page } from "@/lib/types";

interface SourceDrawerProps {
  source: Page | null;
  onClose: () => void;
}

function formatContent(raw: string): string {
  return raw
    .replace(/!\[Image \d+:[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^#{1,4}\s+/gm, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

export function SourceDrawer({ source, onClose }: SourceDrawerProps) {
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (source && scrollRef.current) {
      scrollRef.current.scrollTop = 0;
    }
  }, [source]);

  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [onClose]);

  if (!source) return null;

  const url = (source.extra?.url as string) ?? "";
  const cleaned = formatContent(source.content);
  const paragraphs = cleaned
    .split(/\n\n+/)
    .filter((p) => p.trim().length > 20);

  return (
    <div className="source-drawer-backdrop" onClick={onClose}>
      <div
        className="source-drawer"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="source-drawer-header">
          <button className="source-drawer-close" onClick={onClose}>
            ← back
          </button>
          <span className="source-drawer-id">{source.id.slice(0, 8)}</span>
        </div>

        <div className="source-drawer-scroll" ref={scrollRef}>
          <article className="source-drawer-content">
            <h1 className="source-drawer-title">{source.headline}</h1>

            {url && (
              <a
                href={url}
                target="_blank"
                rel="noopener noreferrer"
                className="source-drawer-link"
              >
                {url}
                <span>&thinsp;↗</span>
              </a>
            )}

            <div className="source-drawer-meta">
              {(source.content.length / 1000).toFixed(0)}k chars
              {" · "}
              {paragraphs.length} sections
            </div>

            <div className="source-drawer-body">
              {paragraphs.map((para, i) => (
                <p key={i}>{para}</p>
              ))}
            </div>
          </article>
        </div>
      </div>
    </div>
  );
}
