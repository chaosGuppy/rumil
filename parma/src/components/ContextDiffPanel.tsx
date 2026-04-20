"use client";

import { useEffect, useMemo, useState } from "react";

import {
  fetchCallEvents,
  fetchPagesByIds,
  type TraceEvent,
} from "@/lib/api";
import type { Page } from "@/lib/types";
import { useInspectPanel } from "./InspectPanelContext";
import { NodeTypeLabel } from "./NodeTypeLabel";

// ContextDiffPanel — answers the question "what did this call see that its
// parent didn't (and vice versa)?" without making the operator eyeball two
// walls of UUIDs in raw context_built JSON.
//
// Design notes
//
// - Always-on inline section under the call detail head, between the
//   meta block and the Events section. No toggle: the audit calls out
//   friction, and an always-visible diff is faster than a hidden one.
//   When the diff is empty (root calls, missing context_built event,
//   or identical contexts), the section collapses to a single dim line.
// - Compares against the direct parent only — the DISPATCHED BY pointer.
//   Walking ancestors is murkier ("which ancestor?") and adds depth on
//   what's already a dense view.
// - "In context" = working_context_page_ids + preloaded_page_ids. Both
//   are pages the LLM saw; collapsing them into one set is what an
//   operator actually wants to compare across calls.
// - Each row shows: short id (clickable → openInspect) · type badge ·
//   headline · which bucket it came from on the *child* call.

interface ContextDiffPanelProps {
  parentCallId: string | null;
  callEvents: TraceEvent[] | null;
}

interface ContextSnapshot {
  // page id → bucket (working / preloaded). We keep the bucket so the
  // added-rows can show whether a page came in via the working set or
  // the preload tier, which often hints at *why* a given page surfaced.
  byId: Map<string, "working" | "preloaded">;
}

function coerceIds(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  const out: string[] = [];
  for (const item of raw) {
    if (typeof item === "string") {
      out.push(item);
    } else if (item && typeof item === "object" && "id" in item) {
      const id = (item as { id: unknown }).id;
      if (typeof id === "string") out.push(id);
    }
  }
  return out;
}

function snapshotFromEvents(events: TraceEvent[] | null): ContextSnapshot | null {
  if (!events) return null;
  const ctx = events.find((e) => e.event === "context_built");
  if (!ctx) return null;
  const working = coerceIds(ctx.working_context_page_ids);
  const preloaded = coerceIds(ctx.preloaded_page_ids);
  const byId = new Map<string, "working" | "preloaded">();
  for (const id of preloaded) byId.set(id, "preloaded");
  // Working takes precedence — if the same id appears in both, we surface
  // it as "working" since that's the strongly-relevant bucket.
  for (const id of working) byId.set(id, "working");
  return { byId };
}

export function ContextDiffPanel({
  parentCallId,
  callEvents,
}: ContextDiffPanelProps) {
  const [parentEvents, setParentEvents] = useState<TraceEvent[] | null>(null);
  const [parentErr, setParentErr] = useState<string | null>(null);
  const [pagesById, setPagesById] = useState<Record<string, Page>>({});
  const [pagesErr, setPagesErr] = useState<string | null>(null);

  useEffect(() => {
    if (!parentCallId) {
      setParentEvents(null);
      setParentErr(null);
      return;
    }
    let cancelled = false;
    setParentEvents(null);
    setParentErr(null);
    fetchCallEvents(parentCallId)
      .then((e) => {
        if (!cancelled) setParentEvents(e);
      })
      .catch((e) => {
        if (!cancelled) setParentErr(e?.message ?? "failed");
      });
    return () => {
      cancelled = true;
    };
  }, [parentCallId]);

  const childCtx = useMemo(() => snapshotFromEvents(callEvents), [callEvents]);
  const parentCtx = useMemo(
    () => snapshotFromEvents(parentEvents),
    [parentEvents],
  );

  const { added, dropped, sharedCount } = useMemo(() => {
    if (!childCtx || !parentCtx) {
      return { added: [], dropped: [], sharedCount: 0 };
    }
    const a: { id: string; bucket: "working" | "preloaded" }[] = [];
    const d: { id: string; bucket: "working" | "preloaded" }[] = [];
    let shared = 0;
    for (const [id, bucket] of childCtx.byId.entries()) {
      if (parentCtx.byId.has(id)) shared += 1;
      else a.push({ id, bucket });
    }
    for (const [id, bucket] of parentCtx.byId.entries()) {
      if (!childCtx.byId.has(id)) d.push({ id, bucket });
    }
    return { added: a, dropped: d, sharedCount: shared };
  }, [childCtx, parentCtx]);

  // One batched fetch covers both added + dropped page ids. The same set
  // is recomputed any time the diff itself changes.
  useEffect(() => {
    const ids = [...new Set([...added.map((r) => r.id), ...dropped.map((r) => r.id)])];
    if (ids.length === 0) {
      setPagesById({});
      setPagesErr(null);
      return;
    }
    let cancelled = false;
    setPagesErr(null);
    fetchPagesByIds(ids)
      .then((m) => {
        if (!cancelled) setPagesById(m);
      })
      .catch((e) => {
        if (!cancelled) setPagesErr(e?.message ?? "failed");
      });
    return () => {
      cancelled = true;
    };
    // We key on a stable join of the ids — added/dropped are fresh arrays
    // each render even when the underlying ids haven't changed.
  }, [
    added.map((r) => r.id).join(","),
    dropped.map((r) => r.id).join(","),
  ]);

  const headerCount = added.length + dropped.length;

  return (
    <section className="trace-detail-section">
      <h2 className="trace-detail-section-title">
        Context diff vs parent
        <span className="trace-detail-section-count">
          {parentCallId ? headerCount : "-"}
        </span>
        {parentCallId && (added.length > 0 || dropped.length > 0) && (
          <span className="ctx-diff-shared" title="pages shared with parent">
            {sharedCount} shared
          </span>
        )}
      </h2>

      {!parentCallId && (
        <div className="trace-detail-dim">
          Root call — no parent context to diff against.
        </div>
      )}
      {parentCallId && parentErr && (
        <div className="trace-detail-err">
          Failed to load parent events: {parentErr}
        </div>
      )}
      {parentCallId &&
        !parentErr &&
        (parentEvents === null || callEvents === null) && (
          <div className="trace-detail-dim">loading diff…</div>
        )}
      {parentCallId &&
        callEvents !== null &&
        parentEvents !== null &&
        !childCtx && (
          <div className="trace-detail-dim">
            This call has no context_built event — nothing to diff.
          </div>
        )}
      {parentCallId &&
        callEvents !== null &&
        parentEvents !== null &&
        childCtx &&
        !parentCtx && (
          <div className="trace-detail-dim">
            Parent call has no context_built event — nothing to diff.
          </div>
        )}
      {parentCallId &&
        childCtx &&
        parentCtx &&
        added.length === 0 &&
        dropped.length === 0 && (
          <div className="trace-detail-dim">
            Identical context to parent ({sharedCount} pages).
          </div>
        )}
      {pagesErr && (
        <div className="trace-detail-err">
          Failed to resolve page details: {pagesErr}
        </div>
      )}

      {(added.length > 0 || dropped.length > 0) && (
        <div className="ctx-diff-grid">
          <DiffColumn
            kind="added"
            label="Added in this call"
            rows={added}
            pagesById={pagesById}
          />
          <DiffColumn
            kind="dropped"
            label="Dropped from parent"
            rows={dropped}
            pagesById={pagesById}
          />
        </div>
      )}
    </section>
  );
}

interface DiffRow {
  id: string;
  bucket: "working" | "preloaded";
}

function DiffColumn({
  kind,
  label,
  rows,
  pagesById,
}: {
  kind: "added" | "dropped";
  label: string;
  rows: DiffRow[];
  pagesById: Record<string, Page>;
}) {
  // Only show the bucket column when the rows actually differ. A column
  // where every row is "working" conveys nothing — drop the tag and say it
  // once in the header instead. Same for all-preloaded.
  const uniformBucket =
    rows.length > 0 && rows.every((r) => r.bucket === rows[0].bucket)
      ? rows[0].bucket
      : null;
  return (
    <div className={`ctx-diff-col ctx-diff-col-${kind}`}>
      <div className="ctx-diff-col-head">
        <span className="ctx-diff-marker" aria-hidden>
          {kind === "added" ? "+" : "−"}
        </span>
        <span className="ctx-diff-col-label">{label}</span>
        <span className="ctx-diff-col-count">{rows.length}</span>
        {uniformBucket && (
          <span
            className={`ctx-diff-col-bucket ctx-diff-col-bucket-${uniformBucket}`}
            title={`all rows are in the ${uniformBucket} bucket`}
          >
            {uniformBucket}
          </span>
        )}
      </div>
      {rows.length === 0 ? (
        <div className="ctx-diff-empty">none</div>
      ) : (
        <ul className="ctx-diff-list">
          {rows.map((row) => (
            <DiffRow
              key={row.id}
              row={row}
              page={pagesById[row.id] ?? null}
              showBucket={uniformBucket === null}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function DiffRow({
  row,
  page,
  showBucket,
}: {
  row: DiffRow;
  page: Page | null;
  showBucket: boolean;
}) {
  const { openInspect } = useInspectPanel();
  const shortId = row.id.slice(0, 8);
  return (
    <li className="ctx-diff-row">
      <button
        type="button"
        className="ctx-diff-id"
        onClick={() => openInspect(shortId)}
        title={`Inspect ${shortId}`}
      >
        {shortId}
      </button>
      {page ? (
        <NodeTypeLabel type={page.page_type} />
      ) : (
        <span className="ctx-diff-type-pending">·</span>
      )}
      <span className="ctx-diff-headline">
        {page?.headline ?? <span className="ctx-diff-pending">resolving…</span>}
      </span>
      {showBucket && (
        <span className={`ctx-diff-bucket ctx-diff-bucket-${row.bucket}`}>
          {row.bucket}
        </span>
      )}
    </li>
  );
}
