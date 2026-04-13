"use client";

import { useState, useRef, useEffect } from "react";
import type { NodeLink, LinkType } from "@/lib/types";

const LINK_CONFIG: Record<LinkType, { label: string; color: string; icon: string }> = {
  supports: { label: "supports", color: "var(--link-supports)", icon: "+" },
  opposes: { label: "opposes", color: "var(--link-opposes)", icon: "−" },
  depends_on: { label: "depends", color: "var(--link-depends)", icon: "→" },
  related: { label: "related", color: "var(--link-related)", icon: "~" },
};

interface LinkGroup {
  type: LinkType;
  links: NodeLink[];
  direction: "out" | "in";
}

function groupLinks(linksOut: NodeLink[], linksIn: NodeLink[]): LinkGroup[] {
  const groups: LinkGroup[] = [];
  const typeOrder: LinkType[] = ["depends_on", "supports", "opposes", "related"];

  for (const type of typeOrder) {
    const out = linksOut.filter((l) => l.link_type === type);
    if (out.length > 0) groups.push({ type, links: out, direction: "out" });
    const incoming = linksIn.filter((l) => l.link_type === type);
    if (incoming.length > 0) groups.push({ type, links: incoming, direction: "in" });
  }

  return groups;
}

function LinkDetail({ link, direction }: { link: NodeLink; direction: "out" | "in" }) {
  const config = LINK_CONFIG[link.link_type];
  return (
    <div className="link-detail-row">
      <span className="link-detail-icon" style={{ color: config.color }}>
        {config.icon}
      </span>
      <span className="link-detail-id">
        {direction === "out" ? link.target_id.slice(0, 8) : link.source_id.slice(0, 8)}
      </span>
      {link.strength !== null && (
        <span className="link-detail-strength">s{link.strength}</span>
      )}
      {link.reasoning && (
        <span className="link-detail-reasoning">{link.reasoning}</span>
      )}
    </div>
  );
}

interface LinkBadgesProps {
  linksOut?: NodeLink[];
  linksIn?: NodeLink[];
}

export function LinkBadges({ linksOut = [], linksIn = [] }: LinkBadgesProps) {
  const [expanded, setExpanded] = useState(false);
  const wrapRef = useRef<HTMLSpanElement>(null);
  const groups = groupLinks(linksOut, linksIn);

  useEffect(() => {
    if (!expanded) return;
    function handleOutside(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setExpanded(false);
      }
    }
    document.addEventListener("mousedown", handleOutside);
    return () => document.removeEventListener("mousedown", handleOutside);
  }, [expanded]);

  if (groups.length === 0) return null;

  return (
    <span className="link-badges-wrap" ref={wrapRef}>
      <button
        className="link-badges"
        onClick={(e) => {
          e.stopPropagation();
          setExpanded(!expanded);
        }}
        title="Node links"
      >
        {groups.map((g) => {
          const config = LINK_CONFIG[g.type];
          const dirLabel = g.direction === "in" ? " ←" : "";
          return (
            <span
              key={`${g.type}-${g.direction}`}
              className="link-badge"
              style={{ color: config.color }}
            >
              <span className="link-badge-icon">{config.icon}</span>
              <span className="link-badge-count">{g.links.length}</span>
              {dirLabel && <span className="link-badge-dir">{dirLabel}</span>}
            </span>
          );
        })}
      </button>

      {expanded && (
        <div className="link-popover">
          {groups.map((g) => {
            const config = LINK_CONFIG[g.type];
            return (
              <div key={`${g.type}-${g.direction}`} className="link-popover-group">
                <div className="link-popover-header" style={{ color: config.color }}>
                  {config.label}{g.direction === "in" ? " (incoming)" : ""}
                </div>
                {g.links.map((link) => (
                  <LinkDetail key={link.id} link={link} direction={g.direction} />
                ))}
              </div>
            );
          })}
        </div>
      )}
    </span>
  );
}
