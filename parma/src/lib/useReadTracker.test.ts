// Tests for the pure helpers that back useReadTracker (see
// src/lib/readTracker.ts). These express the "debouncing" contract of the
// hook — don't count dwells under 3s, don't double-count rapid re-opens
// of the same page — without dragging React or ./api into the test runner.
//
// Invocation (mirrors api.test.ts):
//
//   node --test --experimental-strip-types parma/src/lib/useReadTracker.test.ts

import test from "node:test";
import assert from "node:assert/strict";

import {
  DwellAccumulator,
  MIN_DWELL_SECONDS,
  shouldFlushRead,
} from "./readTracker.ts";

test("DwellAccumulator sums visible intervals across pause/resume", () => {
  const acc = new DwellAccumulator();
  acc.start(1_000);
  acc.pause(3_000); // 2s visible
  acc.start(10_000);
  acc.pause(12_500); // +2.5s
  const total = acc.finish(12_500);
  assert.equal(total, 4.5);
});

test("DwellAccumulator: paused time doesn't count", () => {
  const acc = new DwellAccumulator();
  acc.start(0);
  acc.pause(1_000); // 1s visible
  // Now the tab sits hidden for a long stretch; no start() calls.
  const total = acc.finish(60_000);
  assert.equal(total, 1);
});

test("DwellAccumulator.finish is idempotent", () => {
  const acc = new DwellAccumulator();
  acc.start(0);
  const first = acc.finish(5_000);
  const second = acc.finish(10_000);
  assert.equal(first, 5);
  // finish() is one-shot so repeat callers can't inflate the total
  // (e.g. a lingering visibility listener after unmount).
  assert.equal(second, 0);
});

test("shouldFlushRead ignores dwells under the 3s threshold", () => {
  const flushed = new Set<string>();
  assert.equal(shouldFlushRead("pg1", 2.9, { flushed }), false);
  assert.equal(flushed.has("pg1"), false);
});

test("shouldFlushRead emits once on a qualifying dwell", () => {
  const flushed = new Set<string>();
  assert.equal(shouldFlushRead("pg1", 4, { flushed }), true);
  assert.equal(flushed.has("pg1"), true);
});

test("shouldFlushRead debounces rapid re-opens of the same page", () => {
  // Simulates: user opens a view-item drawer, reads for 5s, closes it, and
  // immediately re-opens for another 5s. Only the first qualifies.
  const flushed = new Set<string>();
  assert.equal(shouldFlushRead("pg1", 5, { flushed }), true);
  assert.equal(shouldFlushRead("pg1", 5, { flushed }), false);
  assert.equal(shouldFlushRead("pg1", 30, { flushed }), false);
});

test("shouldFlushRead dedups per-page, not globally", () => {
  const flushed = new Set<string>();
  assert.equal(shouldFlushRead("pg1", 4, { flushed }), true);
  assert.equal(shouldFlushRead("pg2", 4, { flushed }), true);
  assert.equal(shouldFlushRead("pg1", 4, { flushed }), false);
});

test("MIN_DWELL_SECONDS is the documented 3s threshold", () => {
  // A regression guard: lowering this silently would drop the "only
  // count actual reads, not hover-throughs" contract the backend cares
  // about.
  assert.equal(MIN_DWELL_SECONDS, 3);
});
