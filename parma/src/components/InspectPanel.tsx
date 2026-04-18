"use client";

import { useEffect, useRef, useState } from "react";
import {
  fetchPageByShortId,
  fetchPageDetail,
  type LinkedPage,
  type PageDetail,
} from "@/lib/api";
import type { Page } from "@/lib/types";
import { PageContent } from "./PageContent";
import { CredenceBadge } from "./CredenceBadge";
import { NodeTypeLabel } from "./NodeTypeLabel";

interface InspectPanelProps {
  shortId: string | null;
  onClose: () => void;
  onOpen: (shortId: string) => void;
}

interface LoadState {
  loading: boolean;
  detail: PageDetail | null;
  error: string | null;
}

// The inspect panel is mounted once at the app shell. It opens whenever any
// consumer calls useInspectPanel().openInspect(shortId) — typically from a
// node-ref click inside rendered page content.
export function InspectPanel({ shortId, onClose, onOpen }: InspectPanelProps) {
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
      </div>

      <h2 className="inspect-headline">{page.headline}</h2>

      {page.content && (
        <PageContent
          text={page.content}
          onNodeRef={onOpenRef}
          excludeConceptId={page.id}
        />
      )}

      {page.abstract && page.abstract !== page.content && (
        <div className="inspect-abstract">
          <div className="inspect-section-label">Abstract</div>
          <p>{page.abstract}</p>
        </div>
      )}

      {linksFrom.length > 0 && (
        <section className="inspect-links">
          <div className="inspect-section-label">
            Outgoing links · {linksFrom.length}
          </div>
          <ul>
            {linksFrom.map((lp) => (
              <InspectLinkRow
                key={lp.link.id}
                linked={lp}
                direction="from"
                onOpenRef={onOpenRef}
              />
            ))}
          </ul>
        </section>
      )}

      {linksTo.length > 0 && (
        <section className="inspect-links">
          <div className="inspect-section-label">
            Referenced by · {linksTo.length}
          </div>
          <ul>
            {linksTo.map((lp) => (
              <InspectLinkRow
                key={lp.link.id}
                linked={lp}
                direction="to"
                onOpenRef={onOpenRef}
              />
            ))}
          </ul>
        </section>
      )}

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
      </footer>
    </article>
  );
}

function InspectLinkRow({
  linked,
  direction,
  onOpenRef,
}: {
  linked: LinkedPage;
  direction: "from" | "to";
  onOpenRef: (shortId: string) => void;
}) {
  const { page, link } = linked;
  const shortId = page.id.slice(0, 8);
  const rel = link.direction ? ` (${link.direction})` : "";
  const verb = direction === "from" ? link.link_type : `${link.link_type}-of`;

  return (
    <li className="inspect-link-row">
      <button
        type="button"
        className="node-ref-link inspect-link-id"
        onClick={() => onOpenRef(shortId)}
        title={`Inspect ${shortId}`}
      >
        {shortId}
      </button>
      <span className="inspect-link-verb">
        {verb}
        {rel}
      </span>
      <span className="inspect-link-headline">{page.headline}</span>
    </li>
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
