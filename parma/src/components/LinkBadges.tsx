"use client";

import type { PageLink, ConsiderationDirection } from "@/lib/types";

const DIRECTION_CONFIG: Record<ConsiderationDirection, { label: string; color: string; icon: string }> = {
  supports: { label: "supports", color: "var(--link-supports)", icon: "+" },
  opposes: { label: "opposes", color: "var(--link-opposes)", icon: "−" },
  neutral: { label: "neutral", color: "var(--link-related)", icon: "~" },
};

const LINK_TYPE_CONFIG: Record<string, { label: string; color: string; icon: string }> = {
  consideration: { label: "consideration", color: "var(--fg-dim)", icon: "·" },
  child_question: { label: "sub-question", color: "var(--node-question)", icon: "?" },
  depends_on: { label: "depends", color: "var(--link-depends)", icon: "→" },
  cites: { label: "cites", color: "var(--node-evidence)", icon: "§" },
  answers: { label: "answers", color: "var(--node-judgement)", icon: "⊢" },
  related: { label: "related", color: "var(--link-related)", icon: "~" },
  supersedes: { label: "supersedes", color: "var(--fg-dim)", icon: "↑" },
};

interface LinkBadgesProps {
  links?: PageLink[];
}

export function LinkBadges({ links = [] }: LinkBadgesProps) {
  if (links.length === 0) return null;

  return (
    <span className="link-badges-wrap">
      <span className="link-badges">
        {links.map((link) => {
          if (link.link_type === "consideration" && link.direction) {
            const config = DIRECTION_CONFIG[link.direction];
            return (
              <span
                key={link.id}
                className="link-badge"
                style={{ color: config.color }}
                title={config.label}
              >
                <span className="link-badge-icon">{config.icon}</span>
              </span>
            );
          }
          const config = LINK_TYPE_CONFIG[link.link_type] ?? LINK_TYPE_CONFIG.related;
          return (
            <span
              key={link.id}
              className="link-badge"
              style={{ color: config.color }}
              title={config.label}
            >
              <span className="link-badge-icon">{config.icon}</span>
            </span>
          );
        })}
      </span>
    </span>
  );
}
