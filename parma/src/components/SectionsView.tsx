"use client";

import { useState, useMemo, useCallback } from "react";
import type { QuestionView, ViewItem, ViewSection, Page } from "@/lib/types";
import { CredenceBadge } from "./CredenceBadge";
import { LinkBadges } from "./LinkBadges";
import { NodeTypeLabel } from "./NodeTypeLabel";
import { SourceBadge } from "./SourceBadge";
import { PageContent } from "./PageContent";
import { useInspectPanel } from "./InspectPanelContext";

/**
 * SectionsView — Kanban-style columns where each column is a View section
 * and cards are sized by importance.
 *
 * Design choices:
 * - Section order follows `view.sections` from the API (the backend already
 *   emits them in a deliberate order — see `SECTION_DEFS` in views.py).
 *   Reordering in the frontend would fight the backend and produce
 *   inconsistent navigation.
 * - Importance determines the card variant (L5 → full prose, L4 → abstract,
 *   L3 → headline + badges, L2 → one-liner). Everything at importance ≤ 1 or
 *   null collapses into a "low-importance" folder at the bottom of its column.
 * - Clicking any card opens the global InspectPanel via openInspect(shortId).
 */

interface SectionsViewProps {
  view: QuestionView;
  onOpenSource?: (source: Page) => void;
}

const SECTION_LABELS: Record<string, string> = {
  current_position: "Current Position",
  core_findings: "Core Findings",
  live_hypotheses: "Live Hypotheses",
  key_evidence: "Key Evidence",
  key_uncertainties: "Key Uncertainties",
  structural_framing: "Structural Framing",
  supporting_detail: "Supporting Detail",
  promotion_candidates: "Promotion Candidates",
  demotion_candidates: "Demotion Candidates",
  broader_context: "Broader Context",
  confident_views: "Confident Views",
  assessments: "Assessments",
  other: "Other",
};

type CardVariant = "headline" | "oneliner" | "medium" | "large";

function variantForImportance(imp: number | null): CardVariant | "low" {
  if (imp === null || imp <= 1) return "low";
  if (imp === 2) return "oneliner";
  if (imp === 3) return "headline";
  if (imp === 4) return "medium";
  return "large";
}

function directionLabel(item: ViewItem): string | null {
  for (const link of item.links) {
    if (
      link.link_type === "consideration" &&
      link.direction &&
      link.direction !== "neutral"
    ) {
      return link.direction;
    }
  }
  return null;
}

interface AdversarialVerdict {
  claim_holds: boolean;
  stronger_side: "how_true" | "how_false" | "tie";
  confidence: number;
  rationale: string;
}

function readAdversarialVerdict(page: Page): AdversarialVerdict | null {
  const raw = page.extra?.adversarial_verdict;
  if (!raw || typeof raw !== "object") return null;
  const rec = raw as Record<string, unknown>;
  if (
    typeof rec.claim_holds === "boolean" &&
    typeof rec.confidence === "number" &&
    typeof rec.stronger_side === "string" &&
    typeof rec.rationale === "string"
  ) {
    return {
      claim_holds: rec.claim_holds,
      stronger_side: rec.stronger_side as AdversarialVerdict["stronger_side"],
      confidence: rec.confidence,
      rationale: rec.rationale,
    };
  }
  return null;
}

function ImportanceChip({ importance }: { importance: number | null }) {
  if (importance === null || importance <= 0) return null;
  return (
    <span className="sections-imp-chip" title={`Importance ${importance}/5`}>
      <span className="sections-imp-chip-rail">
        {[1, 2, 3, 4, 5].map((n) => (
          <span
            key={n}
            className={`sections-imp-tick ${n <= importance ? "on" : ""}`}
          />
        ))}
      </span>
      <span className="sections-imp-chip-label">L{importance}</span>
    </span>
  );
}

function DirectionTag({ direction }: { direction: string }) {
  return (
    <span
      className="sections-direction-tag"
      style={{
        color:
          direction === "supports"
            ? "var(--link-supports)"
            : "var(--link-opposes)",
      }}
    >
      {direction === "supports" ? "+" : "−"} {direction}
    </span>
  );
}

function VerdictInline({ verdict }: { verdict: AdversarialVerdict }) {
  const holdsColor = verdict.claim_holds
    ? "var(--link-supports)"
    : "var(--link-opposes)";
  return (
    <div className="sections-verdict">
      <div className="sections-verdict-line">
        <span className="sections-verdict-label">adversarial</span>
        <span className="sections-verdict-status" style={{ color: holdsColor }}>
          {verdict.claim_holds ? "claim holds" : "claim breaks"}
        </span>
        <span className="sections-verdict-side">
          {verdict.stronger_side.replace("_", " ")}
        </span>
        <span className="sections-verdict-conf">C{verdict.confidence}</span>
      </div>
      <div className="sections-verdict-rationale">{verdict.rationale}</div>
    </div>
  );
}

function CardShell({
  item,
  variant,
  onOpenSource,
  onInspect,
  children,
}: {
  item: ViewItem;
  variant: CardVariant;
  onOpenSource?: (source: Page) => void;
  onInspect: (id: string) => void;
  children?: React.ReactNode;
}) {
  const page = item.page;
  const direction = directionLabel(item);
  const citedSourceIds = item.links
    .filter((l) => l.link_type === "cites")
    .map((l) => l.to_page_id.slice(0, 8));

  // Stop propagation on interactive children so clicks on badges/sources
  // don't bubble up to the card's open-inspect handler.
  const stop = (e: React.MouseEvent) => e.stopPropagation();

  return (
    <article
      className={`sections-card sections-card-${variant}`}
      data-page-id={page.id}
      onClick={() => onInspect(page.id)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onInspect(page.id);
        }
      }}
    >
      <div className="sections-card-head">
        <NodeTypeLabel type={page.page_type} />
        <ImportanceChip importance={page.importance} />
        <span className="sections-card-id">{page.id.slice(0, 8)}</span>
      </div>

      <h3 className="sections-card-headline">{page.headline}</h3>

      {children}

      <div className="sections-card-meta" onClick={stop}>
        {direction && <DirectionTag direction={direction} />}
        <CredenceBadge
          credence={page.credence}
          robustness={page.robustness}
        />
        {citedSourceIds.length > 0 && (
          <SourceBadge
            sourceIds={citedSourceIds}
            onOpenDrawer={onOpenSource}
          />
        )}
        <LinkBadges links={item.links} />
      </div>
    </article>
  );
}

function SectionsCard({
  item,
  onOpenSource,
}: {
  item: ViewItem;
  onOpenSource?: (source: Page) => void;
}) {
  const { openInspect } = useInspectPanel();
  const onInspect = useCallback(
    (id: string) => openInspect(id.slice(0, 8)),
    [openInspect],
  );
  const variant = variantForImportance(item.page.importance);
  if (variant === "low") return null;

  const verdict = readAdversarialVerdict(item.page);

  if (variant === "large") {
    return (
      <CardShell
        item={item}
        variant="large"
        onOpenSource={onOpenSource}
        onInspect={onInspect}
      >
        <div
          className="sections-card-body sections-card-body-full"
          onClick={(e) => e.stopPropagation()}
        >
          <PageContent
            text={item.page.content}
            excludeConceptId={item.page.id}
          />
        </div>
        {verdict && <VerdictInline verdict={verdict} />}
      </CardShell>
    );
  }

  if (variant === "medium") {
    const abstract = item.page.abstract?.trim();
    return (
      <CardShell
        item={item}
        variant="medium"
        onOpenSource={onOpenSource}
        onInspect={onInspect}
      >
        {abstract && (
          <div
            className="sections-card-body"
            onClick={(e) => e.stopPropagation()}
          >
            <PageContent text={abstract} excludeConceptId={item.page.id} />
          </div>
        )}
        {verdict && <VerdictInline verdict={verdict} />}
      </CardShell>
    );
  }

  if (variant === "oneliner") {
    const abstract = item.page.abstract?.trim();
    return (
      <CardShell
        item={item}
        variant="oneliner"
        onOpenSource={onOpenSource}
        onInspect={onInspect}
      >
        {abstract && (
          <div className="sections-card-oneliner-body">{abstract}</div>
        )}
      </CardShell>
    );
  }

  // headline variant
  return (
    <CardShell
      item={item}
      variant="headline"
      onOpenSource={onOpenSource}
      onInspect={onInspect}
    />
  );
}

function LowImportanceFolder({
  items,
  onOpenSource,
  forceOpen,
}: {
  items: ViewItem[];
  onOpenSource?: (source: Page) => void;
  forceOpen: boolean;
}) {
  const [open, setOpen] = useState(false);
  const { openInspect } = useInspectPanel();
  const isOpen = open || forceOpen;
  if (items.length === 0) return null;

  return (
    <div className={`sections-low ${isOpen ? "open" : ""}`}>
      {!forceOpen && (
        <button
          type="button"
          className="sections-low-toggle"
          onClick={() => setOpen((v) => !v)}
        >
          <span className="sections-low-chevron">{isOpen ? "▾" : "▸"}</span>
          +{items.length} low-importance
          {items.length === 1 ? " item" : " items"}
        </button>
      )}
      {isOpen && (
        <ul className="sections-low-list">
          {items.map((item) => (
            <li key={item.page.id}>
              <button
                type="button"
                className="sections-low-item"
                onClick={() => openInspect(item.page.id.slice(0, 8))}
              >
                <span className="sections-low-headline">
                  {item.page.headline}
                </span>
                <span className="sections-low-id">
                  {item.page.id.slice(0, 8)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function SectionColumn({
  section,
  accentIndex,
  onOpenSource,
}: {
  section: ViewSection;
  accentIndex: number;
  onOpenSource?: (source: Page) => void;
}) {
  const { visible, low } = useMemo(() => {
    const visible: ViewItem[] = [];
    const low: ViewItem[] = [];
    // Sort within-section by importance desc so big cards lead.
    const sorted = [...section.items].sort(
      (a, b) => (b.page.importance ?? 0) - (a.page.importance ?? 0),
    );
    for (const item of sorted) {
      const v = variantForImportance(item.page.importance);
      if (v === "low") low.push(item);
      else visible.push(item);
    }
    return { visible, low };
  }, [section.items]);

  // If the entire column is low-importance, expand the folder by default
  // so it still looks populated instead of a collapsed hint.
  const forceOpenLow = visible.length === 0 && low.length > 0;

  const label = SECTION_LABELS[section.name] ?? section.name;

  return (
    <section
      className="sections-col"
      style={{
        ["--sections-accent" as string]: `var(--active-${accentIndex % 5})`,
      }}
    >
      <header className="sections-col-head">
        <div className="sections-col-label">{label}</div>
        <div className="sections-col-count">{section.items.length}</div>
        {section.description && (
          <div className="sections-col-desc">{section.description}</div>
        )}
      </header>

      <div className="sections-col-body">
        {visible.length === 0 && low.length === 0 && (
          <div className="sections-col-empty">no items</div>
        )}
        {visible.map((item) => (
          <SectionsCard
            key={item.page.id}
            item={item}
            onOpenSource={onOpenSource}
          />
        ))}
        <LowImportanceFolder
          items={low}
          onOpenSource={onOpenSource}
          forceOpen={forceOpenLow}
        />
      </div>
    </section>
  );
}

export function SectionsView({ view, onOpenSource }: SectionsViewProps) {
  const hasAnyItems = view.sections.some((s) => s.items.length > 0);

  if (view.sections.length === 0 || !hasAnyItems) {
    return (
      <div className="sections-layout">
        <header className="sections-header">
          <h1 className="sections-title">{view.question.headline}</h1>
          {view.question.abstract && (
            <p className="sections-subtitle">{view.question.abstract}</p>
          )}
        </header>
        <div className="sections-empty">
          <div className="sections-empty-line">No view built yet</div>
          <div className="sections-empty-sub">
            run <code>create_view</code> on this question to build a structured
            view.
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="sections-layout">
      <header className="sections-header">
        <h1 className="sections-title">{view.question.headline}</h1>
        {view.question.abstract && (
          <p className="sections-subtitle">{view.question.abstract}</p>
        )}
        <div className="sections-meta">
          {view.health.total_pages} pages · depth {view.health.max_depth} ·{" "}
          {view.sections.length} sections
        </div>
      </header>

      <div className="sections-board">
        {view.sections.map((section, i) => (
          <SectionColumn
            key={section.name}
            section={section}
            accentIndex={i}
            onOpenSource={onOpenSource}
          />
        ))}
      </div>
    </div>
  );
}
