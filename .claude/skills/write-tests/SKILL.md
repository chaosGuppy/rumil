---
description: Use this skill any time a user wants NEW test code written. This covers creating unit tests, integration tests, regression tests, or test coverage for anything — functions, modules, CLI flags, API calls, models, moves, logic, or behavior. Trigger when the user says things like "write a test", "add a test", "test this", "add coverage", "make sure X is tested", "write a regression test", or describes specific behavior they want verified in a test. Also trigger when someone describes a bug and asks for a test to capture it. Do NOT trigger for fixing existing broken tests, configuring pytest, reviewing what's already covered, debugging production code, or tasks unrelated to test authorship.
---

# Writing Tests

## Framework & Style

- Use `pytest` with `pytest-mock`. Never use `unittest.mock` directly.
- Write tests as **inline functions** (`def test_...`), not classes.
- Place tests in `tests/`.

## General principles

- **Avoid coupling to implementation details** — in assertions, in setup, and in how the test runs. A good test says "when I do X, outcome Y is observable in the DB / return value". A bad test says "function Z was called with these exact arguments". More generally, a bad test (for example) relies on specific internal functions taking specific arguments to run successfully; a good test does not. The point is to write tests that are resilient to refactors: Ideally, your tests will only fail if the behaviour under test has genuinely stopped working.
- Fixtures in `conftest.py` for reusable setup (pages, calls, DB).

## The `tmp_db` fixture

A `tmp_db` fixture is defined in `tests/conftest.py`. It creates a fresh SQLite database in a temp directory via `init_db`, wraps it in a `DB` instance, and initialises a budget of 100. Use it for any test that needs database access.

## LLM calls: prefer real calls over mocks

When tests touch LLM-dependent code paths, **call the real LLM rather than mocking**. Mocking LLM responses couples tests to response structure and internal handling details — the exact kind of implementation coupling we want to avoid. Note that conftest.py sets an environment variable to ensure that we always call Haiku, so calling LLMs from tests is as fast and cheap as possible. When calling LLMs from tests, be sure that Haiku can do the work you give to it reliably.

To make this practical:

- **Assert only on basic structural outcomes** that should hold as long as the LLM does something remotely reasonable: e.g. "at least one claim was created", "the call completed", "a judgement page exists". Don't assert on specific wording or exact counts.
- **Keep budgets tiny** (1–2 calls) so tests stay fast.
- **Mark LLM-calling tests** with `@pytest.mark.llm` so they can be skipped in CI or fast-feedback loops (`pytest -m "not llm"`).
- If a test truly cannot call an LLM (e.g. testing pure parsing logic), mocking is fine — but mock at the highest boundary possible and use `mocker` from pytest-mock.
