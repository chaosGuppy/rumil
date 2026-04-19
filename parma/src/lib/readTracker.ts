// Pure, framework-free helpers behind useReadTracker. Split out so tests
// can import them without pulling in ./api (which uses extensionless TS
// imports that Node's --experimental-strip-types loader won't resolve).

export const MIN_DWELL_SECONDS = 3;

// Pages for which we've already flushed a "read" event this tab session.
// Keyed by the full page id (the caller is expected to pass page.id, not a
// short prefix, so two pages with the same 8-char prefix don't collide).
const flushedThisSession = new Set<string>();

// Exposed for the React hook (which reads it on re-render) and for tests
// that want to assert about its contents.
export function _getFlushedSet(): Set<string> {
  return flushedThisSession;
}

// Reset module state between test cases.
export function _resetReadTrackerState(): void {
  flushedThisSession.clear();
}

// Core accumulator, driven by an injectable clock.
//
// Contract:
//   - start(now) begins (or resumes) a measurement.
//   - pause(now) freezes the counter, adding the interval to the total.
//   - finish(now) returns the total seconds and marks this instance done.
//     Subsequent calls return 0.
export class DwellAccumulator {
  private totalMs = 0;
  private runningSince: number | null = null;
  private finished = false;

  start(now: number): void {
    if (this.finished) return;
    if (this.runningSince === null) this.runningSince = now;
  }

  pause(now: number): void {
    if (this.finished) return;
    if (this.runningSince !== null) {
      this.totalMs += now - this.runningSince;
      this.runningSince = null;
    }
  }

  finish(now: number): number {
    if (this.finished) return 0;
    this.pause(now);
    this.finished = true;
    return this.totalMs / 1000;
  }
}

// Decide whether a given dwell on a given pageId should emit a read event.
// Keeps the dedup + threshold decision out of the React hook so it's
// unit-testable and its semantics are explicit.
//
// Side effect: when it returns true, it also marks pageId as already
// flushed, so a subsequent call for the same id returns false — this
// captures the "don't double-count rapid re-opens" debouncing contract.
export function shouldFlushRead(
  pageId: string,
  dwellSeconds: number,
  opts: { minSeconds?: number; flushed?: Set<string> } = {},
): boolean {
  const min = opts.minSeconds ?? MIN_DWELL_SECONDS;
  const set = opts.flushed ?? flushedThisSession;
  if (dwellSeconds < min) return false;
  if (set.has(pageId)) return false;
  set.add(pageId);
  return true;
}
