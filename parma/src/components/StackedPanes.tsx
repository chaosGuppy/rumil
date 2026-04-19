"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import type { QuestionView, ViewItem, Page } from "@/lib/types";
import { fetchPageByShortId, fetchPageDetail, type PageDetail } from "@/lib/api";
import { useReadTracker } from "@/lib/useReadTracker";
import { PageCard } from "./PageCard";
import { PageContent } from "./PageContent";
import { CredenceBadge } from "./CredenceBadge";
import { NodeTypeLabel } from "./NodeTypeLabel";
import { ViewItemFlagButton } from "./ViewItemFlagButton";
import { useInspectPanel } from "./InspectPanelContext";

// StackedPanes — URL-driven recursive pane workbench.
//
// The root question is always the implicit leftmost pane. `?panes=<id>.<id>...`
// encodes a dot-separated stack of 8-char short IDs; each becomes one
// additional pane, loaded via the inspect API.
//
// Interactions:
//   - Plain ref click → opens drawer (cheap peek).
//   - Shift/cmd-click a ref → promoteToPane(id), which pushes onto `?panes=`.
//     This is owned by the provider; we register a handler on mount.
//   - Each detail pane has a close (× drops it AND everything to its right)
//     and a pin-rightward affordance (replicates the ref at this depth).
//
// The existing `.pane` CSS scaffolding (flex, overflow-x: auto, depth tints
// via `--active-N`) drives the visual layout. We only touch URL state and
// lazy fetches here.

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
};

const SHORT_ID_RE = /^[0-9a-f]{8}$/;

interface StackedPanesProps {
  view: QuestionView;
  focusNodeId?: string | null;
  onFocusHandled?: () => void;
  onOpenSource?: (source: Page) => void;
}

function parsePaneStack(param: string | null): string[] {
  if (!param) return [];
  return param
    .split(".")
    .map((s) => s.trim().toLowerCase())
    .filter((s) => SHORT_ID_RE.test(s));
}

function serializePaneStack(stack: string[]): string {
  return stack.join(".");
}

export function StackedPanes({
  view,
  focusNodeId,
  onFocusHandled,
  onOpenSource,
}: StackedPanesProps) {
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const { registerPromoteHandler } = useInspectPanel();

  const containerRef = useRef<HTMLDivElement>(null);
  const lastPaneRef = useRef<HTMLDivElement>(null);

  const paneStack = useMemo(
    () => parsePaneStack(searchParams.get("panes")),
    [searchParams],
  );

  const [activeItemId, setActiveItemId] = useState<string | null>(null);
  const [focusedId, setFocusedId] = useState<string | null>(null);

  const allItems = view.sections.flatMap((s) => s.items);

  const updatePaneStack = useCallback(
    (next: string[]) => {
      const params = new URLSearchParams(searchParams.toString());
      if (next.length === 0) {
        params.delete("panes");
      } else {
        params.set("panes", serializePaneStack(next));
      }
      const query = params.toString();
      // push (not replace) so browser back/forward move through pane
      // stack changes — pinning or closing a pane is a deliberate action
      // the user expects to be able to reverse with the back button.
      router.push(`${pathname}${query ? `?${query}` : ""}`, {
        scroll: false,
      });
    },
    [searchParams, router, pathname],
  );

  const pushPane = useCallback(
    (shortId: string, fromDepth?: number) => {
      const id = shortId.slice(0, 8).toLowerCase();
      if (!SHORT_ID_RE.test(id)) return;
      // Truncate everything strictly to the right of `fromDepth` before
      // appending. Depth 0 is the implicit root pane, so panes[0] is at
      // depth 1; `fromDepth = depth of the source pane`.
      const truncateAt =
        fromDepth !== undefined ? Math.max(0, fromDepth) : paneStack.length;
      const next = paneStack.slice(0, truncateAt);
      // Avoid duplicate-as-last push.
      if (next.length > 0 && next[next.length - 1] === id) return;
      next.push(id);
      updatePaneStack(next);
    },
    [paneStack, updatePaneStack],
  );

  const closePaneAt = useCallback(
    (depth: number) => {
      // depth is 1-based over the stack (depth 0 is the implicit root pane,
      // which cannot be closed). Dropping depth D drops index D-1 and
      // everything to its right.
      if (depth <= 0) return;
      updatePaneStack(paneStack.slice(0, depth - 1));
    },
    [paneStack, updatePaneStack],
  );

  // Register ourselves as the promote target. Any shift-click anywhere in
  // the page (node refs in chat, in cards, in drawer itself) routes through
  // InspectPanelProvider.promoteToPane → here.
  useEffect(() => {
    registerPromoteHandler((id) => pushPane(id));
    return () => registerPromoteHandler(null);
  }, [registerPromoteHandler, pushPane]);

  // Focus-scroll from chat: find the matching card in the root pane.
  useEffect(() => {
    if (!focusNodeId) return;
    setFocusedId(focusNodeId);

    const matchIndex = allItems.findIndex(
      (item) =>
        item.page.headline.includes(focusNodeId) ||
        item.page.id.startsWith(focusNodeId),
    );
    if (matchIndex >= 0) {
      setActiveItemId(allItems[matchIndex].page.id);
      setTimeout(() => {
        const el = document.getElementById(`item-${allItems[matchIndex].page.id}`);
        el?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }, 50);
    }

    const timer = setTimeout(() => {
      setFocusedId(null);
      onFocusHandled?.();
    }, 3000);
    return () => clearTimeout(timer);
  }, [focusNodeId, allItems, onFocusHandled]);

  // Scroll the rightmost pane into view whenever the stack grows.
  useEffect(() => {
    if (paneStack.length === 0) return;
    // Defer one frame so the new pane has mounted.
    const raf = requestAnimationFrame(() => {
      lastPaneRef.current?.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
        inline: "end",
      });
    });
    return () => cancelAnimationFrame(raf);
  }, [paneStack.length]);

  return (
    <div ref={containerRef} className="pane-container">
      <RootPane
        view={view}
        activeItemId={activeItemId}
        focusedId={focusedId}
        onSelectItem={(item) => {
          setActiveItemId(item.page.id);
          // Append a pane to the end of the stack — don't truncate existing
          // panes, since the user often wants to add another pane to their
          // workbench rather than destroy what they've built up.
          pushPane(item.page.id.slice(0, 8));
        }}
        onOpenSource={onOpenSource}
      />

      {paneStack.map((shortId, i) => {
        const depth = i + 1;
        const isLast = i === paneStack.length - 1;
        return (
          <DetailPane
            key={`${i}-${shortId}`}
            shortId={shortId}
            depth={depth}
            isLast={isLast}
            lastPaneRef={isLast ? lastPaneRef : undefined}
            onClose={() => closePaneAt(depth)}
            onPromote={(id) => pushPane(id, depth)}
          />
        );
      })}
    </div>
  );
}

function RootPane({
  view,
  activeItemId,
  focusedId,
  onSelectItem,
  onOpenSource,
}: {
  view: QuestionView;
  activeItemId: string | null;
  focusedId: string | null;
  onSelectItem: (item: ViewItem) => void;
  onOpenSource?: (source: Page) => void;
}) {
  return (
    <div className="pane">
      <div style={{ padding: "48px 36px" }}>
        <header style={{ marginBottom: "40px" }}>
          <div className="pane-kicker">workbench · root question</div>
          <h1
            style={{
              fontSize: "28px",
              fontWeight: 400,
              lineHeight: 1.3,
              margin: "0 0 20px 0",
              fontFamily: "var(--font-body-stack)",
              letterSpacing: "-0.01em",
            }}
          >
            {view.question.headline}
          </h1>
          {view.question.abstract && (
            <div
              className="view-prose"
              style={{
                fontSize: "15px",
                color: "var(--fg-muted)",
                borderLeft: "2px solid var(--border)",
                paddingLeft: "16px",
              }}
            >
              <p style={{ margin: 0 }}>{view.question.abstract}</p>
            </div>
          )}
          <div
            style={{
              marginTop: "12px",
              fontFamily: "var(--font-mono-stack)",
              fontSize: "10px",
              color: "var(--fg-dim)",
              letterSpacing: "0.04em",
            }}
          >
            {view.health.total_pages} pages · depth {view.health.max_depth} ·
            shift-click a ref to pin it as a pane →
          </div>
        </header>

        {view.sections.length === 0 ? (
          <div
            style={{
              padding: "48px 0",
              textAlign: "center",
              fontFamily: "var(--font-mono-stack)",
              fontSize: "12px",
              color: "var(--fg-dim)",
              letterSpacing: "0.04em",
              lineHeight: 1.8,
            }}
          >
            No sections yet.
            <br />
            Open a page below or run a research call to start a workbench.
          </div>
        ) : (
          view.sections.map((section, sectionIdx) => (
            <div key={section.name} style={{ marginBottom: "32px" }}>
              <div
                style={{
                  fontFamily: "var(--font-mono-stack)",
                  fontSize: "10px",
                  letterSpacing: "0.08em",
                  textTransform: "uppercase",
                  color: "var(--fg-dim)",
                  marginBottom: "4px",
                }}
              >
                {SECTION_LABELS[section.name] ?? section.name}
              </div>
              <div
                style={{
                  fontSize: "12px",
                  color: "var(--fg-dim)",
                  marginBottom: "16px",
                  fontFamily: "var(--font-mono-stack)",
                  letterSpacing: "0.02em",
                }}
              >
                {section.description}
              </div>
              {section.items.map((item) => (
                <div key={item.page.id} id={`item-${item.page.id}`}>
                  <PageCard
                    page={item.page}
                    links={item.links}
                    onSelect={() => onSelectItem(item)}
                    onOpenSource={onOpenSource}
                    isActive={activeItemId === item.page.id}
                    isFocused={
                      focusedId
                        ? item.page.headline.includes(focusedId) ||
                          item.page.id.startsWith(focusedId)
                        : false
                    }
                    activeDepth={sectionIdx}
                  />
                </div>
              ))}
            </div>
          ))
        )}
      </div>
    </div>
  );
}

interface DetailPaneProps {
  shortId: string;
  depth: number;
  isLast: boolean;
  lastPaneRef?: React.RefObject<HTMLDivElement | null>;
  onClose: () => void;
  onPromote: (shortId: string) => void;
}

function DetailPane({
  shortId,
  depth,
  isLast,
  lastPaneRef,
  onClose,
  onPromote,
}: DetailPaneProps) {
  const [detail, setDetail] = useState<PageDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setDetail(null);
    (async () => {
      const page = await fetchPageByShortId(shortId);
      if (cancelled) return;
      if (!page) {
        setError(`No page found for ${shortId}.`);
        setLoading(false);
        return;
      }
      const d = await fetchPageDetail(page.id);
      if (cancelled) return;
      if (!d) {
        setDetail({ page, links_from: [], links_to: [] });
      } else {
        setDetail(d);
      }
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [shortId]);

  // Track dwell on the pinned pane. Each mounted DetailPane is considered
  // "visible" for as long as it exists — the user pinned it explicitly,
  // and the parma container doesn't do any lazy hiding today. The hook
  // pauses internally when the browser tab is hidden.
  const resolvedPageId = detail?.page.id ?? null;
  useReadTracker(resolvedPageId, resolvedPageId !== null);

  // Depth tint cycles through --active-0..4. Subtract 1 so the first
  // detail pane (depth 1) starts at --active-0 — matching the warm band
  // that `.node-card.node-active` uses inside the root pane.
  const tintIndex = (depth - 1) % 5;

  return (
    <div
      ref={lastPaneRef}
      className={`pane pane-connected${isLast ? " pane-entering" : ""}`}
      style={
        {
          "--active-tint": `var(--active-${tintIndex})`,
        } as React.CSSProperties
      }
      data-depth={depth}
    >
      <div className="pane-detail-inner">
        <header className="pane-detail-header">
          <div className="pane-detail-head-row">
            <span className="pane-detail-depth">
              pane {depth} · {shortId}
            </span>
            <div className="pane-detail-actions">
              <button
                className="pane-detail-btn"
                onClick={() => onPromote(shortId)}
                title="Pin this page again as the new rightmost pane"
              >
                pin →
              </button>
              <button
                className="pane-detail-btn pane-detail-btn-close"
                onClick={onClose}
                title="Close this pane (and anything to its right)"
                aria-label="Close pane"
              >
                ×
              </button>
            </div>
          </div>
          {detail && (
            <div className="pane-detail-title-row">
              <NodeTypeLabel type={detail.page.page_type} />
              {detail.page.importance !== null &&
                detail.page.importance > 0 && (
                  <span className="pane-detail-chip">
                    L{detail.page.importance}
                  </span>
                )}
              <CredenceBadge
                credence={detail.page.credence}
                robustness={detail.page.robustness}
              />
              {detail.page.is_superseded && (
                <span className="pane-detail-chip pane-detail-chip-warn">
                  superseded
                </span>
              )}
              <ViewItemFlagButton pageId={detail.page.id} />
            </div>
          )}
        </header>

        {loading && (
          <div className="pane-detail-status">Resolving {shortId}…</div>
        )}
        {error && (
          <div className="pane-detail-status pane-detail-status-err">
            {error}
            <div className="pane-detail-status-hint">
              Refs are 8-char prefixes of full page ids. Pages from staged
              runs may not be reachable here.
            </div>
          </div>
        )}
        {detail && <DetailPaneBody detail={detail} onPromote={onPromote} />}
      </div>
    </div>
  );
}

function DetailPaneBody({
  detail,
  onPromote,
}: {
  detail: PageDetail;
  onPromote: (shortId: string) => void;
}) {
  const { page, links_from: linksFrom, links_to: linksTo } = detail;
  return (
    <article>
      <h2 className="pane-detail-headline">{page.headline}</h2>

      {page.content && (
        <div style={{ marginBottom: "16px" }}>
          <PageContent text={page.content} excludeConceptId={page.id} />
        </div>
      )}

      {page.abstract && page.abstract !== page.content && (
        <div className="pane-detail-abstract">
          <div className="pane-detail-section-label">Abstract</div>
          <PageContent
            text={page.abstract}
            excludeConceptId={page.id}
            inline
          />
        </div>
      )}

      {linksFrom.length > 0 && (
        <section className="pane-detail-links">
          <div className="pane-detail-section-label">
            Outgoing · {linksFrom.length}
          </div>
          <ul>
            {linksFrom.map((lp) => {
              const sid = lp.page.id.slice(0, 8);
              const rel = lp.link.direction ? ` (${lp.link.direction})` : "";
              return (
                <li key={lp.link.id}>
                  <button
                    type="button"
                    className="pane-detail-link-id node-ref-link"
                    onClick={() => onPromote(sid)}
                    title="Click to open as pane"
                  >
                    {sid}
                  </button>
                  <span className="pane-detail-link-verb">
                    {lp.link.link_type}
                    {rel}
                  </span>
                  <span className="pane-detail-link-headline">
                    {lp.page.headline}
                  </span>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {linksTo.length > 0 && (
        <section className="pane-detail-links">
          <div className="pane-detail-section-label">
            Referenced by · {linksTo.length}
          </div>
          <ul>
            {linksTo.map((lp) => {
              const sid = lp.page.id.slice(0, 8);
              const rel = lp.link.direction ? ` (${lp.link.direction})` : "";
              return (
                <li key={lp.link.id}>
                  <button
                    type="button"
                    className="pane-detail-link-id node-ref-link"
                    onClick={() => onPromote(sid)}
                    title="Click to open as pane"
                  >
                    {sid}
                  </button>
                  <span className="pane-detail-link-verb">
                    {lp.link.link_type}-of{rel}
                  </span>
                  <span className="pane-detail-link-headline">
                    {lp.page.headline}
                  </span>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      <footer className="pane-detail-provenance">
        <span className="pane-detail-prov-mono">{page.id}</span>
        <span>
          {new Date(page.created_at).toLocaleDateString("en-US", {
            year: "numeric",
            month: "short",
            day: "numeric",
          })}
        </span>
        {page.provenance_call_type && (
          <span className="pane-detail-prov-mono">
            via {page.provenance_call_type}
          </span>
        )}
      </footer>
    </article>
  );
}
