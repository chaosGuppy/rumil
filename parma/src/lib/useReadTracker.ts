"use client";

// useReadTracker — debounced read-dwell telemetry for view-items.
//
// Usage:
//
//   useReadTracker(pageId, /* isOpen */ true);
//
// When the hook's inputs say a particular page is currently visible, it
// starts a timer. On unmount, visibility change, or pageId change, it
// flushes the accumulated dwell — and if the user spent at least
// MIN_DWELL_SECONDS on the page, POSTs to `/api/view-items/{id}/read`.
//
// Dedup: once a page has been flushed as read in this tab session, we don't
// log another read event for it. This keeps rapid re-opens (user bounces
// between the drawer and the content a few times in a row) from
// double-counting. The dedup set is module-scoped so it persists across
// hook instances on the same page view but resets on a full reload.
//
// Visibility: listens for the Page Visibility API. When the tab goes
// hidden we pause the counter; when it comes back, we resume. A long
// invisible period never counts as dwell.
//
// Errors: the flush calls `recordViewItemRead` which already swallows
// errors — telemetry must never break the reader UX.
//
// The pure pieces (DwellAccumulator, shouldFlushRead, MIN_DWELL_SECONDS)
// live in ./readTracker so the test file can import them without pulling
// in ./api (which Node's --experimental-strip-types loader can't resolve
// extensionless).

import { useEffect, useRef } from "react";
import { recordViewItemRead } from "./api";
import {
  DwellAccumulator,
  shouldFlushRead,
  _getFlushedSet,
} from "./readTracker";

// Injection point for tests. Normal code path uses the imported
// recordViewItemRead; tests can swap in a spy via _setFlushFn().
type FlushFn = (pageId: string, dwellSeconds: number) => void | Promise<void>;
let flushImpl: FlushFn = recordViewItemRead;

export function _setFlushFn(fn: FlushFn | null): void {
  flushImpl = fn ?? recordViewItemRead;
}

// document.visibilityState wrapper with a test-safe fallback — the Node
// test runner has no `document`, so treat the page as visible there.
function tabIsVisible(): boolean {
  if (typeof document === "undefined") return true;
  return document.visibilityState !== "hidden";
}

export function useReadTracker(
  pageId: string | null | undefined,
  isOpen: boolean,
): void {
  // One accumulator per (pageId, mount) — recreated when either input
  // changes. Held in a ref so the visibilitychange handler sees the live
  // instance without re-subscribing every render.
  const accRef = useRef<DwellAccumulator | null>(null);

  useEffect(() => {
    if (!pageId || !isOpen) return;
    if (_getFlushedSet().has(pageId)) return;

    const acc = new DwellAccumulator();
    accRef.current = acc;
    if (tabIsVisible()) acc.start(Date.now());

    function onVisibilityChange() {
      if (tabIsVisible()) acc.start(Date.now());
      else acc.pause(Date.now());
    }

    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVisibilityChange);
    }

    return () => {
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVisibilityChange);
      }
      const seconds = acc.finish(Date.now());
      accRef.current = null;
      if (!pageId) return;
      if (!shouldFlushRead(pageId, seconds)) return;
      // Fire-and-forget — recordViewItemRead swallows errors.
      void flushImpl(pageId, seconds);
    };
  }, [pageId, isOpen]);
}

// Re-export the pure helpers so consumers can keep one import path.
export { DwellAccumulator, shouldFlushRead } from "./readTracker";
export { MIN_DWELL_SECONDS, _resetReadTrackerState } from "./readTracker";
