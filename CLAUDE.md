## What This Is

An LLM-powered research workspace. Users pose questions, and the system investigates them by making structured LLM calls (find_considerations, assess, prioritize, ingest) that produce "pages" (claims, questions, judgements, concepts). Pages link together into a research graph with considerations bearing on questions. The codebase is optimised for experimentation, containing multiple implementations of pluggable abstractions, rather than being a monolithic application where there's only one way of achieving things.

**The page graph can be cyclic.** Any code that traverses page links (parent/child questions, considerations, etc.) must track visited nodes to avoid infinite recursion. Use a `_visited: set[str]` parameter — see `context.py` for the standard pattern.

## Staged Runs and the Mutation Log

**Do not break this pattern. All workspace mutations must be stageable.**

Runs can be **staged** (`staged=True` on the `DB` instance), meaning their effects are invisible to other runs and the wider workspace. This supports A/B testing and isolated experimentation.

**How it works:**

- **New pages/links** created by a staged run are written to their base tables with `staged=true` and tagged with `run_id`. Only that run can see them (via `_staged_filter()`).
- **Mutations to existing state** (superseding pages, deleting links, changing link roles) are **always** recorded as append-only events in the `mutation_events` table, regardless of whether the run is staged. Non-staged runs also apply the mutation directly to base tables (dual-write) so that other readers see the change immediately. On read, `_apply_page_events()` / `_apply_link_events()` replay events to materialize a staged run's view. The `MutationState` cache (`_load_mutation_state()`) avoids re-reading events.
- **Retroactive staging:** because all runs record mutation events, a completed non-staged run can be retroactively staged via `DB.stage_run(run_id)`. This flips the run's rows to `staged=true` and reverts direct mutations using the event log, restoring baseline state for other readers.
- **Visibility rule:** staged runs see `staged=false` (baseline) rows plus their own `run_id` rows. Non-staged runs see only baseline. Two staged runs are fully isolated.

**What this means for new code:**

- Any new operation that modifies workspace state (pages, links, or future tables) **must** record a mutation event **and** apply the direct mutation when `not self.staged`. Write new rows with `staged`/`run_id` flags.
- Any new read path **must** apply `_staged_filter()` and the relevant `_apply_*_events()` methods so staged runs see their own mutations.
- RPC functions that read pages or links must accept a `staged_run_id` parameter and apply the same visibility logic. See `match_pages()` and `get_root_questions()` in the migrations for examples.
- Never introduce a write path that silently bypasses event recording — it will break retroactive staging.
- Whenever you make a change, think through whether the research instance LLMs need to know about it - and if so, make sure to edit the relevant prompts.
- If you change data structures, think through what will be needed in terms of database migrations for existing research,

## Running

Environment managed with `uv`.

```bash
# New investigation
uv run python main.py "Your question here" --budget 20

# Use production database (any command)
uv run python main.py --prod "Your question here" --budget 20

# List questions
uv run python main.py --list

# Generate executive summary
uv run python main.py --summary QUESTION_ID

# Use a named workspace to isolate investigations
uv run python main.py "Your question here" --workspace my-project --budget 10

# List all workspaces
uv run python main.py --list-workspaces

# Smoke test (reduced agent rounds, minimal budget)
uv run python main.py "Your question here" --workspace test-scratch --smoke-test
```

`--smoke-test` caps agent loop rounds at a sensible number, making runs fast and cheap while leaving enough for the full flow to be exercised. When running smoke tests, don't override `--budget`.

Tests: `uv run pytest`.

**Database:** Runs against local Supabase by default (`supabase start`). Pass `--prod` to any command to target production. Migrations live in `supabase/migrations/`; CI runs `supabase db push` against prod on merge to `main` (see `.github/workflows/ci.yml`), so it's not necessary to push manually.
Always use the supabase cli to create new migrations: `supabase migration new`.

**Row-Level Security:** RLS is enabled on all tables with **no policies**. This means:

- `service_role` (used by all backend code) bypasses RLS entirely — no performance impact, full access.
- `anon` and `authenticated` roles are denied all table access by PostgreSQL's implicit-deny default.
- The frontend anon key is only used for Realtime broadcast subscriptions, which do not touch the database.

If you add a new table, always enable RLS: `ALTER TABLE new_table ENABLE ROW LEVEL SECURITY;`
Do NOT add "allow all" policies. If you need non-service-role access, add targeted policies.

## Architecture

**Entry point:** `main.py` — CLI arg parsing, dispatches to command functions.

**Single-call runner** (`scripts/run_call.py`): Runs one call type against the local database. Supports `--workspace`, `--smoke-test`, `--ab` (A/B testing with `.a.env`/`.b.env`), `--name`, and `--up-to-stage` (truncate the call lifecycle after `build_context` or `update_workspace`). Runs are recorded in the `runs` table with captured config.

**Package:** `src/rumil/` — installed as `rumil` via hatch/uv. Uses src layout. Always use absolute imports (e.g. `from rumil.database import DB`).

**Orchestrators** (`src/rumil/orchestrators/`): Package of orchestrator implementations (`base.py`, `common.py`, `two_phase.py`, `claim_investigation.py`, `experimental.py`, `robustify.py`). Orchestrators determine the sequence of calls to dispatch. Each represents a different way of prioritizing and executing research.

**Call types** (`src/rumil/calls/`): Composition-based architecture using three pluggable stage ABCs. `CallRunner` (in `stages.py`) orchestrates the three phases by delegating to `ContextBuilder`, `WorkspaceUpdater`, and `ClosingReviewer` instances. Each call type lives in its own module (`find_considerations.py`, `assess.py`, `ingest.py`, etc.) as a thin `CallRunner` subclass. `common.py` has shared utilities (`run_agent_loop()`, `run_single_call()`, closing reviews). Public API re-exported from `__init__.py`.

Architecture:

- `CallRunner` (`stages.py`) — base class for all call types. Owns `run()` orchestration via `CallInfra` (bundles `CallTrace`, `MoveState`, DB, call). Subclasses set class-level `context_builder_cls`, `workspace_updater_cls`, `closing_reviewer_cls`, and `call_type` attributes, plus override `_make_*()` factory methods for parameterized stages and `task_description()`.
- `ContextBuilder` ABC — `build_context(infra) -> ContextResult`. Implementations in `context_builders.py`: `EmbeddingContext`, `IngestEmbeddingContext`, `ScoutEmbeddingContext`, `WebResearchEmbeddingContext`, `BigAssessContext`.
- `WorkspaceUpdater` ABC — `update_workspace(infra, context) -> UpdateResult`. Reusable implementations in `page_creators.py`: `SimpleAgentLoop` (single-pass), `MultiRoundLoop` (multi-round with fruit checks), `WebResearchLoop` (server tools + scraping). Call-specific implementations may live in the call's own module (e.g. `LinkerWorkspaceUpdater` in `link_subquestions.py`).
- `ClosingReviewer` ABC — `closing_review(infra, context, creation) -> None` (persists all results as side effects). Implementations in `closing_reviewers.py`: `StandardClosingReview`, `IngestClosingReview`, `WebResearchClosingReview`, `SinglePhaseScoutReview`.
- Data types (`stages.py`): `CallInfra` (shared infra), `ContextResult` (context output), `UpdateResult` (workspace update output).

The three phases:

1. **build_context** — `ContextBuilder.build_context()` returns `ContextResult`
2. **update_workspace** — `WorkspaceUpdater.update_workspace()` returns `UpdateResult`
3. **closing_review** — `ClosingReviewer.closing_review()` persists results and calls `mark_call_completed()`

To add a new call type: subclass `CallRunner`. Set `call_type`, override `_make_context_builder()`, `_make_workspace_updater()`, `_make_closing_reviewer()`, and `task_description()`. For simple calls, reuse `SimpleAgentLoop` + `StandardClosingReview` + an existing context builder. Export the class from `__init__.py`.

**Assess call variants** (`src/rumil/calls/call_registry.py`): Only the assess call has multiple variants. `ASSESS_CALL_CLASSES = {"default": AssessCall, "big": BigAssessCall}` is keyed by `settings.assess_call_variant`. All other call types use a single concrete class — orchestrators import them directly.

**Available moves** (`src/rumil/available_moves.py`): Named mappings from `CallType` to `Sequence[MoveType]`, controlling which tools each call type can use. `PRESETS` dict holds all presets; `get_moves_for_call()` reads the active preset from `settings.available_moves` and **raises `ValueError` if the preset has no entry for the given call type** — every call type used with a preset must be explicitly listed. `CallRunner._resolve_available_moves()` is a straight passthrough to `get_moves_for_call(self.call_type)`. CLI flag: `--available-moves`.

**LLM interface** (`src/rumil/llm.py`): Wraps the Anthropic API. Provides `call_api()` (single API call with tool handling), `structured_call()` (structured output), and `text_call()`. The multi-turn agent loop lives in `calls/common.py` (`run_agent_loop()`). For single-turn tool-calling use `run_single_call()` — do NOT use `run_agent_loop` with `max_rounds=1`. Both support `messages` for conversation resumption and `cache=True` for prompt caching. When multiple LLM calls share a conversation prefix, pass the same tools to all of them (even if the prompt only asks the model to use a subset) so the cache prefix matches. Builds prompts from `src/rumil/prompts/`: system = preamble.md + call-type-specific .md file, user = context + task. Has retry logic for transient errors.

**Prompt structure** (`src/rumil/prompts/`): bundled inside the package so they ship in non-editable installs (e.g. the API container). All call sites import `PROMPTS_DIR` from `rumil.prompts`. `preamble.md` defines the workspace model, page types, and epistemic conventions shared across all call types. Each call type has its own prompt file (find_considerations.md, assess.md, ingest.md, etc.). Prioritization is driven by phase-specific prompts (`two_phase_initial_prioritization.md`, `two_phase_main_phase_prioritization.md`, `claim_investigation_p1.md`, `claim_investigation_p2.md`) passed as the required `system_prompt` arg to `run_prioritization_call`.

**Moves** (`src/rumil/moves/`): Package with one module per move type. Each module defines a pydantic payload schema, an `execute()` function, and a `MoveDef` that binds them together as a tool. `base.py` has shared helpers (page creation, linking, `LAST_CREATED` resolution). `registry.py` collects all moves into a `MOVES` dict keyed by `MoveType`. See `MoveType` enum in `models.py` for the full list.

**Data layer** (`src/rumil/database.py`): Supabase (Postgres) via the `supabase` Python SDK. Tables: pages, page_links, calls, budget, page_ratings, page_flags, mutation_events, runs, projects. `DB.create(run_id, prod=True, staged=False)` classmethod handles connection setup (delegated to `settings.py`); defaults to local Supabase. When `staged=True`, writes tag rows with `staged`/`run_id` and mutations go to `mutation_events` (see "Staged Runs and the Mutation Log" above). Several operations use Postgres RPC functions defined in the migrations.

**Projects vs Workspace enum** — two separate concepts with confusingly similar names:

- **Project** (`projects` table, `project_id` FK): The user-facing isolation mechanism. The CLI `--workspace <name>` flag resolves to a `Project` row via `db.get_or_create_project(name)`, then `db.project_id` is set. Every query on pages/calls/runs/links filters by `project_id`, so projects are fully isolated from each other.
- **Workspace enum** (`models.Workspace`): An internal layer within a project — `RESEARCH` (default, normal pages), `PRIORITIZATION` (prioritization call outputs). This is orthogonal to project_id; every page has both.

**Data models** (`src/rumil/models.py`): Pydantic BaseModels for Page, PageLink, Call, Project. Used directly as both internal models and FastAPI response types (no separate `*Out` duplicates). Fields with defaults use `_all_fields_required` schema helper so they appear required in the OpenAPI spec. Key enums: PageType, CallType, CallStage, LinkType, ConsiderationDirection, MoveType. MoveType is the source of truth for valid moves — the moves registry maps each to its `MoveDef`. `DISPATCHABLE_CALL_TYPES` defines which `CallType`s prioritization can dispatch (find*considerations, assess, the scout*\* family, and web_research) — the dispatch tool validates against it and the orchestrator dispatches on `CallType` enum values. Recursion dispatches (prioritization/claim-investigation sub-cycles) are not in this set; they are added separately via `RECURSE_DISPATCH_DEF` / `RECURSE_CLAIM_DISPATCH_DEF` passed as `extra_dispatch_defs`.

**Context building** (`src/rumil/context.py`): Assembles LLM context from DB state. `build_prioritization_context()` includes a question index with dispatchable IDs. `build_embedding_based_context()` uses vector similarity search (`embed_query` + `search_pages_by_vector`) to surface the most relevant pages from the entire workspace regardless of graph distance, filling a full-page tier then a summary tier within configurable char budgets (settings: `context_char_budget`, `full_page_char_fraction`, `summary_page_char_fraction`, `distillation_page_char_fraction`). Helper functions include `format_page()` for rendering individual pages and `render_page_and_immediate_children()` for depth-bounded page rendering.

**Tracing** (`src/rumil/tracing/`): Package containing `tracer.py`, `trace_events.py`, and `broadcast.py`. `CallTrace` (in `tracing/tracer.py`) accumulates typed events during a call's lifecycle and persists them as JSONB in the `trace_json` column on `calls`. Events are defined as Pydantic models in `tracing/trace_events.py` with a `Literal` discriminator field (e.g. `event: Literal["context_built"] = "context_built"`). The `TraceEvent` discriminated union is the accepted type for `CallTrace.record()`. API envelope types in `schemas.py` inherit from these events and add `ts`/`call_id` fields. Frontend types are auto-generated. To add a new trace event: (1) define a new Pydantic model in `tracing/trace_events.py` with an `event: Literal["..."] = "..."` field, (2) add it to the `TraceEvent` union, (3) create a corresponding `*EventOut` subclass in `schemas.py` that inherits from both the event and `_TraceEnvelopeMixin`, (4) add it to the `TraceEventOut` union, (5) regenerate frontend types with `./scripts/generate-api-types.sh`, (6) handle the new event in the frontend's `EventSection` component in `call-node.tsx`.

**Events** (`src/rumil/events.py`): In-process publish/subscribe bus for workspace lifecycle events (distinct from tracing — this is about extensibility hooks, not call-lifetime diagnostics). Events are Pydantic models subclassing `Event` with a `Literal` discriminator on `event_type` (e.g. `PageCreatedEvent.event_type: Literal["page_created"]`). Async handlers are registered per concrete event class and invoked sequentially by `fire(event)`. Dispatch is by _exact type_, not `isinstance` — register on the concrete class you care about. A raising handler is logged and swallowed; other handlers still run. Fire events **after** the underlying state has been persisted so handlers observe committed state. Use the bus for _optional_ side effects that extend lifecycle points (e.g. auto-create a View on question creation); use direct calls for mandatory workflow — if A must do X after Y, call X directly rather than hiding it behind an event. Tests should scope registrations with `isolated_bus()` to avoid cross-test leakage. To add a new event type: (1) subclass `Event` in `events.py` with an `event_type: Literal["..."] = "..."` discriminator, (2) add fields describing the event, (3) fire instances from the appropriate lifecycle point after persistence.

**API** (`src/rumil/api/`): FastAPI read-only API for the frontend. Core models from `models.py` are used directly as response types. `schemas.py` defines composite response types (e.g. `CallTraceOut`) and trace event envelope types. `app.py` defines endpoints. Run with `./scripts/dev-api.sh` — this reads the API port from `frontend/.env.overrides` (then `frontend/.env`) so it matches what the frontend expects. To stop the server, read the port from those files (`NEXT_PUBLIC_API_URL`) and kill the process on that port.

**Frontend** (`frontend/`): Next.js TypeScript app with Tailwind. Uses pnpm. Run with `cd frontend && pnpm dev`. The frontend port mirrors the API port: if the API is on `localhost:800X`, the frontend will be on `localhost:300X` (Next.js auto-increments when the default port is taken). Use this to find and stop the frontend process. TypeScript types in `frontend/src/api/` are auto-generated from the API's OpenAPI schema — **never create or edit these files by hand**. When API schemas change, these need to be regenerated with `./scripts/generate-api-types.sh` (or `cd frontend && pnpm generate-api`). This is the only mechanism for sharing types between backend and frontend; do not manually duplicate type definitions. When `schemas.py` or `models.py` is edited, `./scripts/generate-api-types.sh` is automatically run via a hook.

## Versus

`versus/` is a pairwise LLM eval harness on longform web essays (forethought.org, redwoodresearch.substack.com, joecarlsmith.com — pluggable per-source fetchers under `versus/src/versus/sources/`) with a bridge into rumil's agent/orchestrator machinery. Full docs in **`versus/AGENT.md`** — read it before editing any of:

- `versus/` (library, scripts, data)
- `src/rumil/versus_bridge.py` (rumil ↔ versus bridge)
- `src/rumil/api/versus_router.py` (API routes serving `/versus` UI)
- `frontend/src/app/versus/` (UI pages)
- `.claude/skills/rumil-versus-judge/` (CC invocation skill)
- `prompts/versus-*.md` (judge prompt shell + essay-adapted rumil dimensions)

Load-bearing invariants you shouldn't break in a passing edit:

- **Blind judging**: no source_id (can literally be `"human"`) in any agent-visible surface — Question page headline/content, `page.extra`, inline user prompts.
- **Dedup discipline**: completions and judgments live in `versus_texts` / `versus_judgments` (Postgres) keyed on generated content hashes (`request_hash` / `judge_inputs_hash`) — editing a prompt template, sampling param, or any code the fingerprint covers naturally forks the hash and lands a new row. No `*_PROMPT_VERSION` constants. The runner decides "skip if exists" via `versus_db.find_*` queries; replicates (e.g. temperature>0 sampling) are first-class.
- **Bridge model**: `judge_pair_orch` takes `model` explicitly; don't reintroduce `settings.model` reads there. (The earlier `judge_pair_ws_aware` path was removed; historical `rumil:ws:*` rows are read-only-preserved.)

## Key Conventions

- **NEVER pass `--prod` when running `main.py` unless the user explicitly asks you to.** The production database contains real research data. Default to the local database for all testing, development, and exploratory runs.
- **Never run `supabase db reset`** — this wipes the database and is destructive. To apply pending migrations, use `supabase migration up` instead. If you find yourself wanting to reset the database, stop and ask the user first.
- Always scope your test runs to a temp/scratch workspace, e.g. `uv run main.py "When will TAI emerge?" --workspace scratch`

- **Settings precedence: dotenv beats shell env.** `Settings` overrides pydantic-settings' default source order so `.env` / `.env.overrides` win over exported shell variables (see `settings.py:settings_customise_sources`). This is intentional — per-worktree `.env.overrides` is the canonical override mechanism. If a value seems wrong, check the dotenv files before assuming `export FOO=...` took effect.

- Always use absolute imports: `from rumil.module import name` (no relative imports)
- Always put imports at the top of the file, not inside functions
- Use modern type syntax: `X | None` not `Optional[X]`, `list[str]` not `List[str]`, etc. No `from typing import Optional, List, Dict`.
- Prefer `Sequence` over `list` in type hints for parameters and return types. Only use `list` where mutation (e.g. `.append()`) is actually needed. If you find yourself needing to convert sequences to lists at runtime in order to follow this rule, check whether the consuming function really needs to be annotated with a list in its signature; if not, convert the consuming arg it a `Sequence` instead of converting to a list at runtime.
- Multiline strings use parenthesized concatenation of single-quoted lines (`"line 1 " "line 2"`), not triple-quoted strings (`"""`). Only use `f""` on lines that actually contain `{placeholder}` expressions.
- Do not add section divider comments (e.g. `# ----------` banners). Use blank lines between logical sections; the code should speak for itself.
- When adding new user-facing CLI flags or commands to `main.py`, always update `README.md` with corresponding documentation.

## Database query efficiency

**Think hard about query patterns whenever you write code that touches the DB**, and especially when traversing the page graph (children, parents, considerations, judgements, link chains, etc.). The codebase has accumulated many hot loops that issue one query per node and then complain about latency — do not add more.

Before writing a new traversal or graph-walking helper, ask:

- **How many round trips will this make in the worst case?** If it's O(N) in the number of pages/links visited, that's almost always wrong. Aim for O(depth) or O(1) round trips.
- **Can I batch?** Most existing single-page helpers (`get_links_from`, `get_page`, `get_child_questions`, etc.) have or should have a `*_many` counterpart that takes a `Sequence[str]` and issues one `in_(...)` query plus one `_apply_*_events` pass. If the batched helper doesn't exist yet, add it next to the singular one in `database.py` rather than calling the singular form in a loop.
- **Can I do level-by-level BFS instead of recursive per-node fetches?** For depth-bounded subgraph walks, BFS with batched fetches per level gives `O(depth)` round trips regardless of fan-out. See `src/rumil/workspace_exploration/explore.py` for the canonical pattern (one batched links query + one batched pages query per level).
- **Would a single RPC be dramatically better?** Sometimes yes — but remember that any new RPC reading pages or links must accept `staged_run_id` and reproduce the staged-runs visibility logic in SQL (see "Staged Runs and the Mutation Log"). That's real work; only take it on when batching in Python isn't enough.

When you spot an existing per-node loop while working in nearby code, flag it to the user — don't silently rewrite it, but don't pretend you didn't see it either.

## Hooks

PostToolUse hooks in `.claude/settings.json` run automatically after file edits. Do not manually invoke these — they fire on every Edit/Write/MultiEdit:

- **Python lint+format:** `ruff check --fix` and `ruff format` on edited `.py` files.
- **Python type-check:** `uv run pyright` (project-wide) on any `.py` edit.
- **TypeScript type-check:** `npx tsc --noEmit` in `frontend/` on any `.ts`/`.tsx` edit.
- **Frontend type regeneration:** `./scripts/generate-api-types.sh` when `schemas.py` or `models.py` is edited. Do not manually run the type generation script after editing these files — the hook handles it.

## User Interaction

Whenever you run a script that prints a trace url, please report that trace url to the user immediately so they can follow along.

## Skills

You must always invoke the relevant skill when doing certain types of work

- **Writing unit tests** Always invoke the write-tests skill
- **Writing frontend code** Always invoke the frontend-design skill
  When you write a plan that involes either of these things, always include a reminder to invoke the appropriate skill.
