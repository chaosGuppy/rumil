// Tests for the Inlay postMessage schema validation.
//
// Runs with Node 22's built-in test runner + --experimental-strip-types,
// matching parma/src/lib/api.test.ts. Invocation:
//
//   node --test --experimental-strip-types parma/src/components/InlayFrame.test.ts
//
// Scope: only the pure validator. The iframe lifecycle, router push,
// and inspect-panel dispatch happen in React land and are out of
// scope for a Node harness. A browser-level test would need a
// headless DOM (jsdom/playwright); we skip that for MVP.
//
// The validator lives in lib/inlayProtocol.ts so it can be imported
// as a plain .ts module (strip-types doesn't load .tsx). The test
// file stays under components/ so it's next to the component it
// guards — move it at your peril, the lint hook will hunt you down.

/// <reference types="node" />

import test from "node:test";
import assert from "node:assert/strict";

import { validateInlayMessage } from "../lib/inlayProtocol.ts";

test("validates a well-formed ready message", () => {
  const result = validateInlayMessage({ type: "rumil.inlay.v1:ready" });
  assert.deepEqual(result, { type: "rumil.inlay.v1:ready" });
});

test("validates a well-formed resize message", () => {
  const result = validateInlayMessage({
    type: "rumil.inlay.v1:resize",
    height: 640,
  });
  assert.deepEqual(result, {
    type: "rumil.inlay.v1:resize",
    height: 640,
  });
});

test("validates a well-formed open_inspect message", () => {
  const result = validateInlayMessage({
    type: "rumil.inlay.v1:open_inspect",
    page_id: "abc12345",
  });
  assert.deepEqual(result, {
    type: "rumil.inlay.v1:open_inspect",
    page_id: "abc12345",
  });
});

test("validates a well-formed navigate message", () => {
  const result = validateInlayMessage({
    type: "rumil.inlay.v1:navigate",
    page_id: "abc12345",
  });
  assert.deepEqual(result, {
    type: "rumil.inlay.v1:navigate",
    page_id: "abc12345",
  });
});

test("validates a well-formed open_chat with optional fields omitted", () => {
  const result = validateInlayMessage({ type: "rumil.inlay.v1:open_chat" });
  assert.deepEqual(result, {
    type: "rumil.inlay.v1:open_chat",
    question_id: undefined,
    prompt: undefined,
  });
});

test("validates a well-formed open_chat with both fields", () => {
  const result = validateInlayMessage({
    type: "rumil.inlay.v1:open_chat",
    question_id: "ques0001",
    prompt: "Walk me through this",
  });
  assert.deepEqual(result, {
    type: "rumil.inlay.v1:open_chat",
    question_id: "ques0001",
    prompt: "Walk me through this",
  });
});

test("validates a well-formed flag_page message", () => {
  const result = validateInlayMessage({
    type: "rumil.inlay.v1:flag_page",
    page_id: "abc12345",
    reason: "seems stale",
  });
  assert.deepEqual(result, {
    type: "rumil.inlay.v1:flag_page",
    page_id: "abc12345",
    reason: "seems stale",
  });
});

test("rejects null", () => {
  assert.equal(validateInlayMessage(null), null);
});

test("rejects undefined", () => {
  assert.equal(validateInlayMessage(undefined), null);
});

test("rejects non-object", () => {
  assert.equal(validateInlayMessage("hello"), null);
  assert.equal(validateInlayMessage(42), null);
  assert.equal(validateInlayMessage(true), null);
});

test("rejects objects with no type", () => {
  assert.equal(validateInlayMessage({}), null);
  assert.equal(validateInlayMessage({ height: 42 }), null);
});

test("rejects messages without the rumil.inlay.v1 prefix", () => {
  assert.equal(
    validateInlayMessage({ type: "some.other.protocol:ready" }),
    null,
  );
  assert.equal(validateInlayMessage({ type: "rumil.inlay.v2:ready" }), null);
  assert.equal(validateInlayMessage({ type: "ready" }), null);
});

test("rejects messages with unknown kind", () => {
  assert.equal(
    validateInlayMessage({ type: "rumil.inlay.v1:delete_everything" }),
    null,
  );
});

test("rejects resize with non-numeric height", () => {
  assert.equal(
    validateInlayMessage({ type: "rumil.inlay.v1:resize", height: "640" }),
    null,
  );
  assert.equal(
    validateInlayMessage({ type: "rumil.inlay.v1:resize" }),
    null,
  );
});

test("rejects resize with NaN or Infinity height", () => {
  assert.equal(
    validateInlayMessage({ type: "rumil.inlay.v1:resize", height: NaN }),
    null,
  );
  assert.equal(
    validateInlayMessage({
      type: "rumil.inlay.v1:resize",
      height: Infinity,
    }),
    null,
  );
});

test("rejects resize with out-of-range height", () => {
  assert.equal(
    validateInlayMessage({ type: "rumil.inlay.v1:resize", height: -10 }),
    null,
  );
  assert.equal(
    validateInlayMessage({
      type: "rumil.inlay.v1:resize",
      height: 1_000_000,
    }),
    null,
  );
});

test("rejects open_inspect without page_id", () => {
  assert.equal(
    validateInlayMessage({ type: "rumil.inlay.v1:open_inspect" }),
    null,
  );
});

test("rejects open_inspect with non-string page_id", () => {
  assert.equal(
    validateInlayMessage({
      type: "rumil.inlay.v1:open_inspect",
      page_id: 12345,
    }),
    null,
  );
});

test("rejects open_inspect with empty page_id", () => {
  assert.equal(
    validateInlayMessage({
      type: "rumil.inlay.v1:open_inspect",
      page_id: "",
    }),
    null,
  );
});

test("rejects open_inspect with overlong page_id", () => {
  assert.equal(
    validateInlayMessage({
      type: "rumil.inlay.v1:open_inspect",
      page_id: "x".repeat(200),
    }),
    null,
  );
});

test("rejects navigate without page_id", () => {
  assert.equal(
    validateInlayMessage({ type: "rumil.inlay.v1:navigate" }),
    null,
  );
});

test("rejects flag_page missing required fields", () => {
  assert.equal(
    validateInlayMessage({ type: "rumil.inlay.v1:flag_page" }),
    null,
  );
  assert.equal(
    validateInlayMessage({
      type: "rumil.inlay.v1:flag_page",
      page_id: "abc12345",
    }),
    null,
  );
  assert.equal(
    validateInlayMessage({
      type: "rumil.inlay.v1:flag_page",
      reason: "x",
    }),
    null,
  );
});

test("rejects flag_page with overlong reason", () => {
  assert.equal(
    validateInlayMessage({
      type: "rumil.inlay.v1:flag_page",
      page_id: "abc12345",
      reason: "x".repeat(3000),
    }),
    null,
  );
});

test("rejects open_chat with non-string prompt", () => {
  assert.equal(
    validateInlayMessage({
      type: "rumil.inlay.v1:open_chat",
      prompt: 42,
    }),
    null,
  );
});

test("rejects open_chat with overlong prompt", () => {
  assert.equal(
    validateInlayMessage({
      type: "rumil.inlay.v1:open_chat",
      prompt: "x".repeat(5000),
    }),
    null,
  );
});
