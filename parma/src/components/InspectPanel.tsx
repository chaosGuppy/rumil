"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  fetchPageByShortId,
  fetchPageDetail,
  type LinkedPage,
  type PageDetail,
} from "@/lib/api";
import { useReadTracker } from "@/lib/useReadTracker";
import type { LinkType } from "@/lib/types";
import { PageContent } from "./PageContent";
import { CredenceBadge } from "./CredenceBadge";
import { IterationDiffPanel } from "./IterationDiffPanel";
import { NodeTypeLabel } from "./NodeTypeLabel";
import { PageAnnotationActions } from "./PageAnnotationActions";
import { ViewItemFlagButton } from "./ViewItemFlagButton";
import { useInspectPanel } from "./InspectPanelContext";

interface InspectPanelProps {
  shortId: string | null;
  onClose: () => void;
  onOpen: (shortId: string) => void;
  onPromote?: (shortId: string) => void;
}

interface LoadState {
  loading: boolean;
  detail: PageDetail | null;
  error: string | null;
}

// The inspect panel is mounted once at the app shell. It opens whenever any
// consumer calls useInspectPanel().openInspect(shortId) — typically from a
// node-ref click inside rendered page content.
export function InspectPanel({
  shortId,
  onClose,
  onOpen,
  onPromote,
}: InspectPanelProps) {
  const [state, setState] = useState<LoadState>({
    loading: false,
    detail: null,
    error: null,
  });
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!shortId) {
      setState({ loading: false, detail: null, error: null });
      return;
    }
    let cancelled = false;
    setState({ loading: true, detail: null, error: null });

    (async () => {
      const page = await fetchPageByShortId(shortId);
      if (cancelled) return;
      if (!page) {
        setState({
          loading: false,
          detail: null,
          error: `No page found for ${shortId}.`,
        });
        return;
      }
      const detail = await fetchPageDetail(page.id);
      if (cancelled) return;
      if (!detail) {
        // Fallback: we still have the page, just no links surfaced.
        setState({
          loading: false,
          detail: { page, links_from: [], links_to: [] },
          error: null,
        });
        return;
      }
      setState({ loading: false, detail, error: null });
    })();

    return () => {
      cancelled = true;
    };
  }, [shortId]);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  }, [shortId]);

  // Record a read event once the user dwells on a resolved page for >= 3s.
  // Keyed on the full page id (not the short id) so two pages sharing a
  // short-id prefix don't collide in the dedup set.
  const resolvedPageId = state.detail?.page.id ?? null;
  useReadTracker(resolvedPageId, shortId !== null && resolvedPageId !== null);

  useEffect(() => {
    if (!shortId) return;
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [shortId, onClose]);

  if (!shortId) return null;

  return (
    <div className="inspect-backdrop" onClick={onClose}>
      <aside
        className="inspect-panel"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="Inspect page"
      >
        <header className="inspect-header">
          <button className="inspect-close" onClick={onClose} title="Close (Esc)">
            close
          </button>
          <span className="inspect-short-id">{shortId}</span>
          {onPromote && (
            <button
              className="inspect-pin"
              onClick={() => onPromote(shortId)}
              title="Pin to panes — push this page as a new rightmost pane"
            >
              pin →
            </button>
          )}
        </header>

        <div className="inspect-scroll" ref={scrollRef}>
          {state.loading && (
            <div className="inspect-status">Resolving {shortId}…</div>
          )}
          {state.error && (
            <div className="inspect-status inspect-status-err">
              {state.error}
              <div className="inspect-status-hint">
                Refs are 8-char prefixes of the full page id. If this page
                was created in a staged run, it won&rsquo;t be visible here.
              </div>
            </div>
          )}
          {state.detail && (
            <InspectBody detail={state.detail} onOpenRef={onOpen} />
          )}
        </div>
      </aside>
    </div>
  );
}

function InspectBody({
  detail,
  onOpenRef,
}: {
  detail: PageDetail;
  onOpenRef: (shortId: string) => void;
}) {
  const { page, links_from: linksFrom, links_to: linksTo } = detail;

  return (
    <article className="inspect-body">
      <div className="inspect-meta-row">
        <NodeTypeLabel type={page.page_type} />
        <CredenceBadge credence={page.credence} robustness={page.robustness} />
        {page.importance !== null && page.importance > 0 && (
          <span className="inspect-meta-chip">L{page.importance}</span>
        )}
        {page.is_superseded && (
          <span className="inspect-meta-chip inspect-meta-chip-warn">
            superseded
          </span>
        )}
        <PageAnnotationActions pageId={page.id} />
        <ViewItemFlagButton pageId={page.id} />
      </div>

      <h2 className="inspect-headline">{page.headline}</h2>

      {page.content && (
        <PageContent
          text={page.content}
          onNodeRef={onOpenRef}
          excludeConceptId={page.id}
          pageId={page.id}
        />
      )}

      {page.abstract && page.abstract !== page.content && (
        <div className="inspect-abstract">
          <div className="inspect-section-label">Abstract</div>
          <p>{page.abstract}</p>
        </div>
      )}

      {page.page_type === "artifact" && <IterationDiffPanel pageId={page.id} />}

      <InspectLinkGroups
        linksFrom={linksFrom}
        linksTo={linksTo}
        onOpenRef={onOpenRef}
      />

      <footer className="inspect-provenance">
        <ProvenanceRow label="id" value={page.id} mono />
        <ProvenanceRow
          label="created"
          value={new Date(page.created_at).toLocaleString("en-US", {
            year: "numeric",
            month: "short",
            day: "numeric",
            hour: "numeric",
            minute: "2-digit",
          })}
        />
        {page.provenance_call_type && (
          <ProvenanceRow label="via" value={page.provenance_call_type} mono />
        )}
        {page.run_id && <InspectTraceLink page={page} />}
      </footer>
    </article>
  );
}

// A single "slice" of links that share a rendering label + direction.
// Multiple slices can belong to one group (e.g. Load-bearing has both
// "depends on" and "depended on by" slices).
interface LinkSlice {
  key: string;
  label: string;
  direction: "from" | "to";
  links: LinkedPage[];
  // When true, render strength bars alongside each row (DEPENDS_ON only).
  showStrength?: boolean;
}

interface LinkGroup {
  key: string;
  title: string;
  slices: LinkSlice[];
  count: number;
  defaultOpen: boolean;
}

function InspectLinkGroups({
  linksFrom,
  linksTo,
  onOpenRef,
}: {
  linksFrom: LinkedPage[];
  linksTo: LinkedPage[];
  onOpenRef: (shortId: string) => void;
}) {
  const groups = useMemo(
    () => buildLinkGroups(linksFrom, linksTo),
    [linksFrom, linksTo],
  );
  if (groups.length === 0) return null;

  return (
    <div className="inspect-groups">
      {groups.map((group) => (
        <InspectLinkGroup
          key={group.key}
          group={group}
          onOpenRef={onOpenRef}
        />
      ))}
    </div>
  );
}

function InspectLinkGroup({
  group,
  onOpenRef,
}: {
  group: LinkGroup;
  onOpenRef: (shortId: string) => void;
}) {
  return (
    <details
      className={`inspect-group inspect-group-${group.key}`}
      open={group.defaultOpen}
    >
      <summary className="inspect-group-summary">
        <span className="inspect-group-caret" aria-hidden />
        <span className="inspect-group-title">{group.title}</span>
        <span className="inspect-group-count">{group.count}</span>
      </summary>
      <div className="inspect-group-body">
        {group.slices.map((slice) => (
          <div key={slice.key} className="inspect-slice">
            {group.slices.length > 1 && (
              <div className="inspect-slice-label">
                {slice.label}
                <span className="inspect-slice-count">
                  {slice.links.length}
                </span>
              </div>
            )}
            <ul className="inspect-link-list">
              {slice.links.map((lp) => (
                <InspectLinkRow
                  key={lp.link.id}
                  linked={lp}
                  direction={slice.direction}
                  onOpenRef={onOpenRef}
                  showStrength={slice.showStrength}
                />
              ))}
            </ul>
          </div>
        ))}
      </div>
    </details>
  );
}

// Sorting helpers: strongest/most-impactful links rise to the top within a slice.
function byStrengthDesc(a: LinkedPage, b: LinkedPage): number {
  return (b.link.strength ?? 0) - (a.link.strength ?? 0);
}

function byImpactDesc(a: LinkedPage, b: LinkedPage): number {
  const ai = a.link.impact_on_parent_question ?? -1;
  const bi = b.link.impact_on_parent_question ?? -1;
  if (ai !== bi) return bi - ai;
  return byStrengthDesc(a, b);
}

function buildLinkGroups(
  linksFrom: LinkedPage[],
  linksTo: LinkedPage[],
): LinkGroup[] {
  const from = (t: LinkType) =>
    linksFrom.filter((lp) => lp.link.link_type === t);
  const to = (t: LinkType) =>
    linksTo.filter((lp) => lp.link.link_type === t);

  const dependsFrom = from("depends_on").slice().sort(byStrengthDesc);
  const dependsTo = to("depends_on").slice().sort(byStrengthDesc);

  const citesFrom = from("cites").slice().sort(byStrengthDesc);
  const citesTo = to("cites").slice().sort(byStrengthDesc);

  const supersedesFrom = from("supersedes");
  const supersedesTo = to("supersedes");
  const variantFrom = from("variant");
  const variantTo = to("variant");

  const considerationFrom = from("consideration");
  const considerationTo = to("consideration");
  const childFrom = from("child_question").slice().sort(byImpactDesc);
  const childTo = to("child_question").slice().sort(byImpactDesc);
  const answersFrom = from("answers");
  const answersTo = to("answers");
  const viewItemFrom = from("view_item");
  const viewItemTo = to("view_item");
  const viewOfFrom = from("view_of");
  const viewOfTo = to("view_of");
  const metaForFrom = from("meta_for");
  const metaForTo = to("meta_for");

  const relatedFrom = from("related");
  const relatedTo = to("related");
  const summarizesFrom = from("summarizes");
  const summarizesTo = to("summarizes");

  const groups: LinkGroup[] = [];

  const loadBearingSlices: LinkSlice[] = [];
  if (dependsFrom.length > 0) {
    loadBearingSlices.push({
      key: "depends-on",
      label: "This depends on",
      direction: "from",
      links: dependsFrom,
      showStrength: true,
    });
  }
  if (dependsTo.length > 0) {
    loadBearingSlices.push({
      key: "depended-on-by",
      label: "What depends on this",
      direction: "to",
      links: dependsTo,
      showStrength: true,
    });
  }
  if (loadBearingSlices.length > 0) {
    groups.push({
      key: "load-bearing",
      title: "Load-bearing",
      slices: loadBearingSlices,
      count: dependsFrom.length + dependsTo.length,
      defaultOpen: true,
    });
  }

  const provenanceSlices: LinkSlice[] = [];
  if (citesFrom.length > 0) {
    provenanceSlices.push({
      key: "cites",
      label: "Cites",
      direction: "from",
      links: citesFrom,
    });
  }
  if (citesTo.length > 0) {
    provenanceSlices.push({
      key: "cited-by",
      label: "Cited by",
      direction: "to",
      links: citesTo,
    });
  }
  if (provenanceSlices.length > 0) {
    groups.push({
      key: "provenance",
      title: "Provenance",
      slices: provenanceSlices,
      count: citesFrom.length + citesTo.length,
      defaultOpen: true,
    });
  }

  const lineageSlices: LinkSlice[] = [];
  if (supersedesFrom.length > 0) {
    lineageSlices.push({
      key: "supersedes",
      label: "Supersedes",
      direction: "from",
      links: supersedesFrom,
    });
  }
  if (supersedesTo.length > 0) {
    lineageSlices.push({
      key: "superseded-by",
      label: "Superseded by",
      direction: "to",
      links: supersedesTo,
    });
  }
  const variantLinks = [...variantFrom, ...variantTo];
  if (variantLinks.length > 0) {
    lineageSlices.push({
      key: "variant",
      label: "Variants",
      direction: "from",
      links: variantLinks,
    });
  }
  const summarizesLinks = [...summarizesFrom, ...summarizesTo];
  if (summarizesLinks.length > 0) {
    lineageSlices.push({
      key: "summarizes",
      label: "Summarizes / summarized by",
      direction: "from",
      links: summarizesLinks,
    });
  }
  if (lineageSlices.length > 0) {
    const lineageCount = lineageSlices.reduce(
      (n, s) => n + s.links.length,
      0,
    );
    groups.push({
      key: "lineage",
      title: "Lineage",
      slices: lineageSlices,
      count: lineageCount,
      defaultOpen: lineageCount <= 3,
    });
  }

  const structureSlices: LinkSlice[] = [];
  if (considerationFrom.length > 0) {
    structureSlices.push({
      key: "consideration-from",
      label: "Bears on",
      direction: "from",
      links: considerationFrom,
    });
  }
  if (considerationTo.length > 0) {
    structureSlices.push({
      key: "consideration-to",
      label: "Considerations",
      direction: "to",
      links: considerationTo,
    });
  }
  if (childFrom.length > 0) {
    structureSlices.push({
      key: "child-question-from",
      label: "Sub-questions",
      direction: "from",
      links: childFrom,
    });
  }
  if (childTo.length > 0) {
    structureSlices.push({
      key: "child-question-to",
      label: "Parent question",
      direction: "to",
      links: childTo,
    });
  }
  if (answersFrom.length > 0) {
    structureSlices.push({
      key: "answers-from",
      label: "Answers",
      direction: "from",
      links: answersFrom,
    });
  }
  if (answersTo.length > 0) {
    structureSlices.push({
      key: "answered-by",
      label: "Answered by",
      direction: "to",
      links: answersTo,
    });
  }
  const viewItemAll = [...viewItemFrom, ...viewItemTo];
  if (viewItemAll.length > 0) {
    structureSlices.push({
      key: "view-item",
      label: "View items",
      direction: "from",
      links: viewItemAll,
    });
  }
  const viewOfAll = [...viewOfFrom, ...viewOfTo];
  if (viewOfAll.length > 0) {
    structureSlices.push({
      key: "view-of",
      label: "View of",
      direction: "from",
      links: viewOfAll,
    });
  }
  const metaForAll = [...metaForFrom, ...metaForTo];
  if (metaForAll.length > 0) {
    structureSlices.push({
      key: "meta-for",
      label: "Meta",
      direction: "from",
      links: metaForAll,
    });
  }
  if (structureSlices.length > 0) {
    const structureCount = structureSlices.reduce(
      (n, s) => n + s.links.length,
      0,
    );
    groups.push({
      key: "structure",
      title: "Structure",
      slices: structureSlices,
      count: structureCount,
      defaultOpen: structureCount <= 5,
    });
  }

  const relatedSlices: LinkSlice[] = [];
  if (relatedFrom.length > 0) {
    relatedSlices.push({
      key: "related-from",
      label: "Related",
      direction: "from",
      links: relatedFrom,
    });
  }
  if (relatedTo.length > 0) {
    relatedSlices.push({
      key: "related-to",
      label: "Related (incoming)",
      direction: "to",
      links: relatedTo,
    });
  }
  if (relatedSlices.length > 0) {
    const relatedCount = relatedSlices.reduce(
      (n, s) => n + s.links.length,
      0,
    );
    groups.push({
      key: "related",
      title: "Related",
      slices: relatedSlices,
      count: relatedCount,
      defaultOpen: relatedCount <= 5,
    });
  }

  return groups;
}

function InspectLinkRow({
  linked,
  direction,
  onOpenRef,
  showStrength,
}: {
  linked: LinkedPage;
  direction: "from" | "to";
  onOpenRef: (shortId: string) => void;
  showStrength?: boolean;
}) {
  const { page, link } = linked;
  const shortId = page.id.slice(0, 8);
  const meta: string[] = [];
  if (link.direction) meta.push(link.direction);
  if (link.role && link.role !== "direct") meta.push(link.role);
  if (link.impact_on_parent_question !== null && link.impact_on_parent_question !== undefined) {
    meta.push(`impact ${link.impact_on_parent_question}`);
  }

  return (
    <li className="inspect-link-row">
      <div className="inspect-link-primary">
        <button
          type="button"
          className="node-ref-link inspect-link-id"
          onClick={() => onOpenRef(shortId)}
          title={`Inspect ${shortId}`}
        >
          {shortId}
        </button>
        <span className="inspect-link-headline">{page.headline}</span>
        {showStrength && (
          <StrengthBars value={link.strength ?? 0} direction={direction} />
        )}
      </div>
      {(meta.length > 0 || link.reasoning) && (
        <div className="inspect-link-detail">
          {meta.length > 0 && (
            <span className="inspect-link-meta">{meta.join(" · ")}</span>
          )}
          {link.reasoning && (
            <span className="inspect-link-reasoning">{link.reasoning}</span>
          )}
        </div>
      )}
    </li>
  );
}

// Render a 0-5 strength as five small dots. Filled dots are tinted with
// the depends-on link color; empty dots stay neutral. An arrow marker
// indicates whether the dependency runs outward (this → target) or
// inward (source → this).
function StrengthBars({
  value,
  direction,
}: {
  value: number;
  direction: "from" | "to";
}) {
  const clamped = Math.max(0, Math.min(5, Math.round(value)));
  const arrow = direction === "from" ? "→" : "←";
  return (
    <span
      className="inspect-strength"
      title={`strength ${clamped}/5`}
      aria-label={`strength ${clamped} of 5`}
    >
      <span className="inspect-strength-arrow" aria-hidden>
        {arrow}
      </span>
      {[0, 1, 2, 3, 4].map((i) => (
        <span
          key={i}
          className={
            i < clamped
              ? "inspect-strength-dot inspect-strength-dot-on"
              : "inspect-strength-dot"
          }
        />
      ))}
    </span>
  );
}

function ProvenanceRow({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="inspect-prov-row">
      <span className="inspect-prov-label">{label}</span>
      <span className={`inspect-prov-value${mono ? " inspect-prov-mono" : ""}`}>
        {value}
      </span>
    </div>
  );
}

// Row-shaped "open in TRACE" action on the provenance footer. Renders as
// a button styled like a ProvenanceRow so the footer keeps its grid
// alignment. The ProvenanceChip on PageCard is the primary entry point,
// but a reader who has drilled into a page via the inspect drawer needs
// the same affordance here.
function InspectTraceLink({ page }: { page: import("@/lib/types").Page }) {
  const { openTrace } = useInspectPanel();
  const runId = page.run_id!;
  return (
    <button
      type="button"
      className="inspect-prov-row inspect-prov-trace"
      onClick={() => openTrace(runId, page.provenance_call_id)}
      title={`View trace for run ${runId.slice(0, 8)}`}
    >
      <span className="inspect-prov-label">trace</span>
      <span className="inspect-prov-value inspect-prov-mono inspect-prov-trace-link">
        → open trace ({runId.slice(0, 8)})
      </span>
    </button>
  );
}
