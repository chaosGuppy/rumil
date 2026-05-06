import Link from "next/link";
import { notFound } from "next/navigation";
import type { PageInstanceCalls, PageTimeline, PageCallRef, PageTimelineEvent } from "@/api";
import { atlasFetch } from "../../_lib/fetch";
import { Crumbs } from "../../_components/Crumbs";
import { fmtCost, fmtWhen, fmtRelative } from "../../_lib/format";

function CallRefRow({ callRef: ref }: { callRef: PageCallRef }) {
  const flowHref = ref.run_id
    ? `/atlas/runs/${ref.run_id}/flow#call-${ref.call_id}`
    : `/atlas/calls/${encodeURIComponent(ref.call_type)}`;
  return (
    <Link href={flowHref} className="atlas-call-list-row">
      <span className="id">{ref.call_id.slice(0, 8)}</span>
      <span className="ct">
        <Link
          href={`/atlas/calls/${encodeURIComponent(ref.call_type)}`}
          style={{ color: "inherit" }}
        >
          {ref.call_type}
        </Link>
        {ref.status && (
          <span
            className={`atlas-chip ${
              ref.status === "complete"
                ? "is-success"
                : ref.status === "error"
                  ? "is-warm"
                  : "is-muted"
            }`}
            style={{ marginLeft: "0.5rem", fontSize: "0.6rem" }}
          >
            {ref.status}
          </span>
        )}
      </span>
      <span style={{ color: "var(--a-muted)", textAlign: "right", fontSize: "0.7rem" }}>
        {ref.created_at ? fmtRelative(ref.created_at) : ""}
        {ref.cost_usd != null && (
          <span style={{ marginLeft: "0.5rem" }}>{fmtCost(ref.cost_usd)}</span>
        )}
      </span>
    </Link>
  );
}

function timelineCls(kind: string): string {
  const k = kind.toLowerCase();
  if (k.includes("create")) return "is-create";
  if (k.includes("supersede")) return "is-supersede";
  if (k.includes("load")) return "is-load";
  if (k.includes("context")) return "is-context";
  return "";
}

function TimelineRow({ ev }: { ev: PageTimelineEvent }) {
  return (
    <div className={`atlas-timeline-event ${timelineCls(ev.kind)}`}>
      <span className="atlas-timeline-when" title={ev.ts}>
        {fmtRelative(ev.ts)}
      </span>
      <div className="atlas-timeline-body">
        <span className="atlas-timeline-kind">{ev.kind}</span>
        {ev.detail && <span className="atlas-timeline-detail">{ev.detail}</span>}
        {ev.call_id && (
          <span style={{ fontFamily: "var(--a-mono)", fontSize: "0.7rem" }}>
            {ev.run_id ? (
              <Link href={`/atlas/runs/${ev.run_id}/flow#call-${ev.call_id}`}>
                {ev.call_id.slice(0, 8)}
              </Link>
            ) : (
              <span style={{ color: "var(--a-muted)" }}>{ev.call_id.slice(0, 8)}</span>
            )}
            {ev.call_type && (
              <>
                {" · "}
                <Link href={`/atlas/calls/${encodeURIComponent(ev.call_type)}`}>
                  {ev.call_type}
                </Link>
              </>
            )}
          </span>
        )}
      </div>
    </div>
  );
}

export async function PageInstanceDetail({ pageId }: { pageId: string }) {
  const [calls, timeline] = await Promise.all([
    atlasFetch<PageInstanceCalls | null>(
      `/api/atlas/pages/${encodeURIComponent(pageId)}/calls`,
      null,
    ),
    atlasFetch<PageTimeline | null>(
      `/api/atlas/pages/${encodeURIComponent(pageId)}/timeline`,
      null,
    ),
  ]);

  if (!calls && !timeline) notFound();

  const headline = calls?.headline || timeline?.headline || pageId.slice(0, 8);
  const pageType = calls?.page_type || timeline?.page_type || "—";
  const inContext = calls?.in_context_of ?? [];
  const loadedBy = calls?.loaded_by ?? [];
  const events = (timeline?.events ?? []).slice().sort((a, b) =>
    a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0,
  );

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs
            items={[
              { label: "atlas", href: "/atlas" },
              { label: "pages", href: "/atlas/pages" },
              { label: pageType, href: `/atlas/pages/${encodeURIComponent(pageType)}` },
              { label: pageId.slice(0, 8) },
            ]}
          />
          <h1>{headline}</h1>
          <div className="atlas-chip-row" style={{ marginBottom: "0.85rem" }}>
            <span className="atlas-chip is-accent">{pageType}</span>
            <span className="atlas-chip is-muted atlas-mono" title={pageId}>
              {pageId}
            </span>
            {calls?.superseded_by_page_id && (
              <Link
                href={`/atlas/pages/${encodeURIComponent(calls.superseded_by_page_id)}`}
                className="atlas-chip is-warm"
              >
                superseded by {calls.superseded_by_page_id.slice(0, 8)} →
              </Link>
            )}
          </div>
          <p className="atlas-lede">
            Provenance for one page: which call created it, which calls have
            seen it in context, and the chronological event log.
          </p>
        </div>
      </div>

      {calls?.superseded_by_page_id && (
        <div className="atlas-callout">
          <strong style={{ display: "block", marginBottom: "0.3rem", fontFamily: "var(--a-mono)", fontSize: "0.7rem", textTransform: "uppercase", letterSpacing: "0.12em" }}>
            superseded
          </strong>
          this revision has been replaced by{" "}
          <Link href={`/atlas/pages/${encodeURIComponent(calls.superseded_by_page_id)}`}>
            <span className="atlas-mono">{calls.superseded_by_page_id}</span>
          </Link>
          .
        </div>
      )}

      <div className="atlas-page-instance-grid">
        <section>
          <div className="atlas-section-head">
            <h2>calls</h2>
            <span className="atlas-section-meta">
              who created and read this page
            </span>
          </div>

          {calls?.created_by_call ? (
            <div className="atlas-call-list" style={{ marginBottom: "1rem" }}>
              <div className="atlas-call-list-role">created by</div>
              <CallRefRow callRef={calls.created_by_call} />
            </div>
          ) : (
            <div className="atlas-call-list" style={{ marginBottom: "1rem" }}>
              <div className="atlas-call-list-role">created by</div>
              <div className="atlas-call-list-empty">no creating call recorded</div>
            </div>
          )}

          <div className="atlas-call-list" style={{ marginBottom: "1rem" }}>
            <div className="atlas-call-list-role">
              in context of ({inContext.length})
            </div>
            {inContext.length === 0 ? (
              <div className="atlas-call-list-empty">never used as context</div>
            ) : (
              inContext.map((c) => <CallRefRow key={`ctx-${c.call_id}`} callRef={c} />)
            )}
          </div>

          <div className="atlas-call-list">
            <div className="atlas-call-list-role">loaded by ({loadedBy.length})</div>
            {loadedBy.length === 0 ? (
              <div className="atlas-call-list-empty">never loaded explicitly</div>
            ) : (
              loadedBy.map((c) => <CallRefRow key={`load-${c.call_id}`} callRef={c} />)
            )}
          </div>
        </section>

        <section>
          <div className="atlas-section-head">
            <h2>timeline</h2>
            <span className="atlas-section-meta">
              {events.length} event{events.length === 1 ? "" : "s"} · newest first
            </span>
          </div>
          {events.length === 0 ? (
            <div className="atlas-empty">
              <strong>no timeline events</strong>
              this page has no recorded lifecycle events.
            </div>
          ) : (
            <div className="atlas-timeline">
              {events.map((ev, i) => (
                <TimelineRow key={`${ev.ts}-${i}`} ev={ev} />
              ))}
            </div>
          )}
        </section>
      </div>

      <div style={{ marginTop: "1.5rem", fontFamily: "var(--a-mono)", fontSize: "0.72rem", color: "var(--a-muted)" }}>
        last seen at <span title={fmtWhen(timeline?.events?.[0]?.ts)}>
          {fmtRelative(timeline?.events?.[0]?.ts)}
        </span>
      </div>
    </div>
  );
}
