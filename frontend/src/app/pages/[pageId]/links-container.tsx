"use client";

import { useState, useMemo } from "react";
import Link from "next/link";
import type { LinkedPageOut, PageLink } from "@/api";

const TYPE_CONFIG: Record<
  string,
  { accent: string; bg: string; bgHover: string; border: string }
> = {
  claim: {
    accent: "var(--type-claim)",
    bg: "var(--type-claim-bg)",
    bgHover: "var(--type-claim-bg-hover)",
    border: "var(--type-claim-border)",
  },
  question: {
    accent: "var(--type-question)",
    bg: "var(--type-question-bg)",
    bgHover: "var(--type-question-bg-hover)",
    border: "var(--type-question-border)",
  },
  judgement: {
    accent: "var(--type-judgement)",
    bg: "var(--type-judgement-bg)",
    bgHover: "var(--type-judgement-bg-hover)",
    border: "var(--type-judgement-border)",
  },
  source: {
    accent: "var(--type-source)",
    bg: "var(--type-source-bg)",
    bgHover: "var(--type-source-bg-hover)",
    border: "var(--type-source-border)",
  },
  concept: {
    accent: "var(--type-concept)",
    bg: "var(--type-concept-bg)",
    bgHover: "var(--type-concept-bg-hover)",
    border: "var(--type-concept-border)",
  },
  wiki: {
    accent: "var(--type-wiki)",
    bg: "var(--type-wiki-bg)",
    bgHover: "var(--type-wiki-bg-hover)",
    border: "var(--type-wiki-border)",
  },
};

function LinkMeta({ link }: { link: PageLink }) {
  return (
    <div className="link-meta">
      <span className="link-type-label">{link.link_type.replace("_", " ")}</span>
      {link.strength > 0 && (
        <span className="link-strength">
          <span className="link-strength-bar">
            <span
              className="link-strength-fill"
              style={{ width: `${(link.strength / 5) * 100}%` }}
            />
          </span>
          {link.strength.toFixed(1)}
        </span>
      )}
    </div>
  );
}

function LinkedCard({ lp, stagedRunId }: { lp: LinkedPageOut; stagedRunId?: string }) {
  const cfg = TYPE_CONFIG[lp.page.page_type] || TYPE_CONFIG.source;
  const isSuperseded = lp.page.is_superseded;
  const href = stagedRunId
    ? `/pages/${lp.page.id}?staged_run_id=${stagedRunId}`
    : `/pages/${lp.page.id}`;
  return (
    <Link
      href={href}
      className={`linked-card${isSuperseded ? " linked-card-superseded" : ""}`}
    >
      <div className="linked-card-accent" style={{ background: cfg.accent }} />
      <div className="linked-card-body">
        <LinkMeta link={lp.link} />
        <div className="linked-card-header">
          <span
            className="linked-card-type"
            style={{ color: cfg.accent, background: cfg.bg }}
          >
            {lp.page.page_type}
          </span>
          <span className="linked-card-id">{lp.page.id.slice(0, 8)}</span>
          {isSuperseded && (
            <span className="linked-card-superseded-badge">superseded</span>
          )}
        </div>
        <div className="linked-card-summary">{lp.page.headline}</div>
        {lp.link.reasoning && (
          <div className="linked-card-reasoning">{lp.link.reasoning}</div>
        )}
      </div>
    </Link>
  );
}

function LinkSection({
  title,
  links,
  showSuperseded,
  stagedRunId,
}: {
  title: string;
  links: LinkedPageOut[];
  showSuperseded: boolean;
  stagedRunId?: string;
}) {
  const visible = (
    showSuperseded ? links : links.filter((lp) => !lp.page.is_superseded)
  ).toSorted(
    (a, b) => new Date(b.page.created_at).getTime() - new Date(a.page.created_at).getTime(),
  );
  if (visible.length === 0) return null;
  return (
    <div className="link-section">
      <div className="link-section-header">
        <span className="link-section-title">{title}</span>
        <span className="link-section-count">{visible.length}</span>
      </div>
      <div className="link-grid">
        {visible.map((lp) => (
          <LinkedCard key={lp.link.id} lp={lp} stagedRunId={stagedRunId} />
        ))}
      </div>
    </div>
  );
}

export default function LinksContainer({
  links_from,
  links_to,
  stagedRunId,
}: {
  links_from: LinkedPageOut[];
  links_to: LinkedPageOut[];
  stagedRunId?: string;
}) {
  const [showSuperseded, setShowSuperseded] = useState(false);

  const supersededCount = useMemo(
    () =>
      [...links_from, ...links_to].filter((lp) => lp.page.is_superseded).length,
    [links_from, links_to],
  );

  const hasSuperseded = supersededCount > 0;

  return (
    <div className="links-container">
      {hasSuperseded && (
        <div className="links-filter-bar">
          <button
            className={`links-superseded-toggle${showSuperseded ? " active" : ""}`}
            onClick={() => setShowSuperseded((prev) => !prev)}
            title="Show superseded pages"
          >
            {showSuperseded ? "hide" : "show"} superseded
            {showSuperseded && supersededCount > 0 && (
              <span className="links-superseded-count">{supersededCount}</span>
            )}
          </button>
        </div>
      )}
      <LinkSection title="Outgoing" links={links_from} showSuperseded={showSuperseded} stagedRunId={stagedRunId} />
      <LinkSection title="Incoming" links={links_to} showSuperseded={showSuperseded} stagedRunId={stagedRunId} />
    </div>
  );
}
