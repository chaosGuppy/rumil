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
import {
  commitRun,
  continueResearch,
  createRootQuestion,
  stageRun,
  startAbEval,
} from "./api.ts";

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

test("stageRun POSTs to /runs/:id/stage with no body", async () => {
  const stub = stubFetch({
    ok: true,
    body: { run_id: "r-1", staged: true },
  });
  try {
    const result = await stageRun("r-1");
    assert.equal(stub.calls.length, 1);
    assert.ok(stub.calls[0].url.endsWith("/api/runs/r-1/stage"));
    assert.equal(stub.calls[0].init.method, "POST");
    assert.deepEqual(result, { run_id: "r-1", staged: true });
  } finally {
    stub.restore();
  }
});

test("stageRun surfaces FastAPI detail on 409", async () => {
  const stub = stubFetch({
    ok: false,
    status: 409,
    body: { detail: "Run is already staged" },
  });
  try {
    await assert.rejects(
      () => stageRun("r-1"),
      (err: Error) => err.message.includes("already staged"),
    );
  } finally {
    stub.restore();
  }
});

test("commitRun POSTs to /runs/:id/commit", async () => {
  const stub = stubFetch({
    ok: true,
    body: { run_id: "r-2", staged: false },
  });
  try {
    const result = await commitRun("r-2");
    assert.equal(stub.calls.length, 1);
    assert.ok(stub.calls[0].url.endsWith("/api/runs/r-2/commit"));
    assert.equal(stub.calls[0].init.method, "POST");
    assert.deepEqual(result, { run_id: "r-2", staged: false });
  } finally {
    stub.restore();
  }
});

test("continueResearch POSTs the budget to the question-scoped continue endpoint", async () => {
  const stub = stubFetch({
    ok: true,
    body: { run_id: "new-run", question_id: "q-99", budget: 7 },
  });
  try {
    const result = await continueResearch("q-99", 7);
    assert.equal(stub.calls.length, 1);
    const call = stub.calls[0];
    assert.ok(call.url.endsWith("/api/questions/q-99/continue"));
    assert.equal(call.init.method, "POST");
    const headers = call.init.headers as Record<string, string>;
    assert.equal(headers["Content-Type"], "application/json");
    assert.deepEqual(JSON.parse(call.init.body as string), { budget: 7 });
    assert.deepEqual(result, { run_id: "new-run", question_id: "q-99", budget: 7 });
  } finally {
    stub.restore();
  }
});

test("continueResearch URL-encodes the question id", async () => {
  const stub = stubFetch({
    ok: true,
    body: { run_id: "x", question_id: "q/slash", budget: 5 },
  });
  try {
    await continueResearch("q/slash", 5);
    assert.ok(
      stub.calls[0].url.includes("/api/questions/q%2Fslash/continue"),
      `expected URL encoding in: ${stub.calls[0].url}`,
    );
  } finally {
    stub.restore();
  }
});

test("startAbEval POSTs both run ids to /ab-evals", async () => {
  const stub = stubFetch({
    ok: true,
    body: { run_id_a: "a", run_id_b: "b", status: "started" },
  });
  try {
    const result = await startAbEval("a", "b");
    assert.equal(stub.calls.length, 1);
    assert.ok(stub.calls[0].url.endsWith("/api/ab-evals"));
    assert.equal(stub.calls[0].init.method, "POST");
    assert.deepEqual(JSON.parse(stub.calls[0].init.body as string), {
      run_id_a: "a",
      run_id_b: "b",
    });
    assert.deepEqual(result, { run_id_a: "a", run_id_b: "b", status: "started" });
  } finally {
    stub.restore();
  }
});

test("startAbEval surfaces FastAPI detail on 400 (same run ids)", async () => {
  const stub = stubFetch({
    ok: false,
    status: 400,
    body: { detail: "run_id_a and run_id_b must differ" },
  });
  try {
    await assert.rejects(
      () => startAbEval("same", "same"),
      (err: Error) => err.message.includes("must differ"),
    );
  } finally {
    stub.restore();
  }
});
