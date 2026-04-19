// Tests for src/lib/api.ts helpers.
//
// Runs with Node 22's built-in test runner + --experimental-strip-types so
// no jest/vitest dependency is needed. Invocation:
//
//   node --test --experimental-strip-types parma/src/lib/api.test.ts
//
// We only cover helpers whose contract is "serialize a URL + body" — the
// shape the backend relies on. Full UI rendering is out of scope; that's
// what the FastAPI endpoint tests (tests/test_create_root_question_endpoint.py)
// and the live dev server cover.

import test from "node:test";
import assert from "node:assert/strict";

// Explicit .ts extension is intentional for Node's strip-types loader; tsc
// forbids it so tests are excluded from the tsconfig `include`.
import { createRootQuestion } from "./api.ts";

type FetchCall = {
  url: string;
  init: RequestInit;
};

function stubFetch(response: { ok: boolean; status?: number; body?: unknown }) {
  const calls: FetchCall[] = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (url: string | URL, init: RequestInit = {}) => {
    calls.push({ url: url.toString(), init });
    return {
      ok: response.ok,
      status: response.status ?? 200,
      json: async () => response.body ?? {},
    } as Response;
  }) as typeof globalThis.fetch;
  return {
    calls,
    restore: () => {
      globalThis.fetch = originalFetch;
    },
  };
}

test("createRootQuestion POSTs to the workspace-scoped questions endpoint", async () => {
  const stub = stubFetch({
    ok: true,
    body: { id: "pg1", headline: "hi" },
  });
  try {
    const result = await createRootQuestion(
      "proj-123",
      "Will this endpoint work?",
      "some longer content",
    );
    assert.equal(stub.calls.length, 1);
    const call = stub.calls[0];
    assert.ok(
      call.url.endsWith("/api/projects/proj-123/questions"),
      `unexpected URL: ${call.url}`,
    );
    assert.equal(call.init.method, "POST");
    const headers = call.init.headers as Record<string, string>;
    assert.equal(headers["Content-Type"], "application/json");
    const parsed = JSON.parse(call.init.body as string);
    assert.deepEqual(parsed, {
      headline: "Will this endpoint work?",
      content: "some longer content",
    });
    assert.deepEqual(result, { id: "pg1", headline: "hi" });
  } finally {
    stub.restore();
  }
});

test("createRootQuestion sends content=null when body is omitted", async () => {
  const stub = stubFetch({ ok: true, body: { id: "pg2" } });
  try {
    await createRootQuestion("workspace-xyz", "Just a headline");
    assert.equal(stub.calls.length, 1);
    const parsed = JSON.parse(stub.calls[0].init.body as string);
    assert.deepEqual(parsed, {
      headline: "Just a headline",
      content: null,
    });
  } finally {
    stub.restore();
  }
});

test("createRootQuestion surfaces the FastAPI detail message on failure", async () => {
  const stub = stubFetch({
    ok: false,
    status: 422,
    body: { detail: "Question headline must not be empty or whitespace-only." },
  });
  try {
    await assert.rejects(
      () => createRootQuestion("pid", "   "),
      (err: Error) => err.message.includes("must not be empty"),
    );
  } finally {
    stub.restore();
  }
});
