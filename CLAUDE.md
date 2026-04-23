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

`--smoke-test` caps agent loop rounds at 2 per call, making runs fast and cheap. Use it for development and manual testing. When running smoke tests, don't override `--budget` unless there's a good reason to.

Tests: `uv run pytest`.

**Database:** Runs against local Supabase by default (`supabase start`). Pass `--prod` to any command to target production. Migrations live in `supabase/migrations/` and are pushed to prod with `supabase db push`.
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

**LLM interface** (`src/rumil/llm.py`): Wraps the Anthropic API. Provides `call_api()` (single API call with tool handling), `structured_call()` (structured output), and `text_call()`. The multi-turn agent loop lives in `calls/common.py` (`run_agent_loop()`). For single-turn tool-calling use `run_single_call()` — do NOT use `run_agent_loop` with `max_rounds=1`. Both support `messages` for conversation resumption and `cache=True` for prompt caching. When multiple LLM calls share a conversation prefix, pass the same tools to all of them (even if the prompt only asks the model to use a subset) so the cache prefix matches. Builds prompts from `prompts/` directory: system = preamble.md + call-type-specific .md file, user = context + task. Has retry logic for transient errors.

**Prompt structure** (`prompts/`): `preamble.md` defines the workspace model, page types, and epistemic conventions shared across all call types. Each call type has its own prompt file (find_considerations.md, assess.md, ingest.md, etc.). Prioritization is driven by phase-specific prompts (`two_phase_initial_prioritization.md`, `two_phase_main_phase_prioritization.md`, `claim_investigation_p1.md`, `claim_investigation_p2.md`) passed as the required `system_prompt` arg to `run_prioritization_call`.

**Moves** (`src/rumil/moves/`): Package with one module per move type. Each module defines a pydantic payload schema, an `execute()` function, and a `MoveDef` that binds them together as a tool. `base.py` has shared helpers (page creation, linking, `LAST_CREATED` resolution). `registry.py` collects all moves into a `MOVES` dict keyed by `MoveType`. See `MoveType` enum in `models.py` for the full list.

**Data layer** (`src/rumil/database.py`): Supabase (Postgres) via the `supabase` Python SDK. Tables: pages, page_links, calls, budget, page_ratings, page_flags, mutation_events, runs, projects. `DB.create(run_id, prod=True, staged=False)` classmethod handles connection setup (delegated to `settings.py`); defaults to local Supabase. When `staged=True`, writes tag rows with `staged`/`run_id` and mutations go to `mutation_events` (see "Staged Runs and the Mutation Log" above). Several operations use Postgres RPC functions defined in the migrations.

**Projects vs Workspace enum** — two separate concepts with confusingly similar names:
- **Project** (`projects` table, `project_id` FK): The user-facing isolation mechanism. The CLI `--workspace <name>` flag resolves to a `Project` row via `db.get_or_create_project(name)`, then `db.project_id` is set. Every query on pages/calls/runs/links filters by `project_id`, so projects are fully isolated from each other.
- **Workspace enum** (`models.Workspace`): An internal layer within a project — `RESEARCH` (default, normal pages), `PRIORITIZATION` (prioritization call outputs). This is orthogonal to project_id; every page has both.

**Data models** (`src/rumil/models.py`): Pydantic BaseModels for Page, PageLink, Call, Project. Used directly as both internal models and FastAPI response types (no separate `*Out` duplicates). Fields with defaults use `_all_fields_required` schema helper so they appear required in the OpenAPI spec. Key enums: PageType, CallType, CallStage, LinkType, ConsiderationDirection, MoveType. MoveType is the source of truth for valid moves — the moves registry maps each to its `MoveDef`. `DISPATCHABLE_CALL_TYPES` defines which `CallType`s prioritization can dispatch (find_considerations, assess, the scout_* family, and web_research) — the dispatch tool validates against it and the orchestrator dispatches on `CallType` enum values. Recursion dispatches (prioritization/claim-investigation sub-cycles) are not in this set; they are added separately via `RECURSE_DISPATCH_DEF` / `RECURSE_CLAIM_DISPATCH_DEF` passed as `extra_dispatch_defs`.

**Context building** (`src/rumil/context.py`): Assembles LLM context from DB state. `build_prioritization_context()` includes a question index with dispatchable IDs. `build_embedding_based_context()` uses vector similarity search (`embed_query` + `search_pages_by_vector`) to surface the most relevant pages from the entire workspace regardless of graph distance, filling a full-page tier then a summary tier within configurable char budgets (settings: `context_char_budget`, `full_page_char_fraction`, `summary_page_char_fraction`, `distillation_page_char_fraction`). Helper functions include `format_page()` for rendering individual pages and `render_page_and_immediate_children()` for depth-bounded page rendering.

**Tracing** (`src/rumil/tracing/`): Package containing `tracer.py`, `trace_events.py`, and `broadcast.py`. `CallTrace` (in `tracing/tracer.py`) accumulates typed events during a call's lifecycle and persists them as JSONB in the `trace_json` column on `calls`. Events are defined as Pydantic models in `tracing/trace_events.py` with a `Literal` discriminator field (e.g. `event: Literal["context_built"] = "context_built"`). The `TraceEvent` discriminated union is the accepted type for `CallTrace.record()`. API envelope types in `schemas.py` inherit from these events and add `ts`/`call_id` fields. Frontend types are auto-generated. To add a new trace event: (1) define a new Pydantic model in `tracing/trace_events.py` with an `event: Literal["..."] = "..."` field, (2) add it to the `TraceEvent` union, (3) create a corresponding `*EventOut` subclass in `schemas.py` that inherits from both the event and `_TraceEnvelopeMixin`, (4) add it to the `TraceEventOut` union, (5) regenerate frontend types with `./scripts/generate-api-types.sh`, (6) handle the new event in the frontend's `EventSection` component in `call-node.tsx`.

**Events** (`src/rumil/events.py`): In-process publish/subscribe bus for workspace lifecycle events (distinct from tracing — this is about extensibility hooks, not call-lifetime diagnostics). Events are Pydantic models subclassing `Event` with a `Literal` discriminator on `event_type` (e.g. `PageCreatedEvent.event_type: Literal["page_created"]`). Async handlers are registered per concrete event class and invoked sequentially by `fire(event)`. Dispatch is by *exact type*, not `isinstance` — register on the concrete class you care about. A raising handler is logged and swallowed; other handlers still run. Fire events **after** the underlying state has been persisted so handlers observe committed state. Use the bus for *optional* side effects that extend lifecycle points (e.g. auto-create a View on question creation); use direct calls for mandatory workflow — if A must do X after Y, call X directly rather than hiding it behind an event. Tests should scope registrations with `isolated_bus()` to avoid cross-test leakage. To add a new event type: (1) subclass `Event` in `events.py` with an `event_type: Literal["..."] = "..."` discriminator, (2) add fields describing the event, (3) fire instances from the appropriate lifecycle point after persistence.

**API** (`src/rumil/api/`): FastAPI read-only API for the frontend. Core models from `models.py` are used directly as response types. `schemas.py` defines composite response types (e.g. `CallTraceOut`) and trace event envelope types. `app.py` defines endpoints. Run with `./scripts/dev-api.sh` — this reads the API port from `frontend/.env.local` so it matches what the frontend expects. To stop the server, read the port from `frontend/.env.local` (`NEXT_PUBLIC_API_URL`) and kill the process on that port.

**Frontend** (`frontend/`): Next.js TypeScript app with Tailwind. Uses pnpm. Run with `cd frontend && pnpm dev`. The frontend port mirrors the API port: if the API is on `localhost:800X`, the frontend will be on `localhost:300X` (Next.js auto-increments when the default port is taken). Use this to find and stop the frontend process. TypeScript types in `frontend/src/api/` are auto-generated from the API's OpenAPI schema — **never create or edit these files by hand**. When API schemas change, these need to be regenerated with `./scripts/generate-api-types.sh` (or `cd frontend && pnpm generate-api`). This is the only mechanism for sharing types between backend and frontend; do not manually duplicate type definitions. When `schemas.py` or `models.py` is edited, `./scripts/generate-api-types.sh` is automatically run via a hook.

## Versus

`versus/` is a pairwise LLM eval harness on forethought.org essays: models continue essay openings ("from-scratch") or paraphrase whole essays (style-controlled baseline), then blind judges compare continuations across criteria. The artifact is a gen-model × judge-model matrix of how often each judge prefers the human continuation. Library lives at `versus/src/versus/`; CLI entry points at `versus/scripts/`; data in `versus/data/`.

Bridge to rumil is `src/rumil/versus_bridge.py` — exposes `judge_pair_ws_aware` (single agent call with workspace tools) and `judge_pair_orch` (full TwoPhaseOrchestrator per pair + closing call). API routes under `/versus` live in `src/rumil/api/versus_router.py`.

### Core invariant: reruns are free

Adding a model, judge, criterion, or prefix-config must **never** re-run existing matching rows. All three stores are keyed on deterministic dedup keys:

| Store | Key composition |
|---|---|
| `data/completions.jsonl` | `essay_id · prefix_config_hash · source_id · sampling_hash` |
| `data/paraphrases.jsonl` | `essay_id · model_id · sampling_hash` |
| `data/judgments.jsonl`   | `essay_id · prefix_hash · sorted(source_a, source_b) · criterion · judge_model` |

`prefix_config_hash` mixes in essay content + prefix params (n_paragraphs, include_headers, length_tolerance) + `prepare.COMPLETION_PROMPT_VERSION`. `sampling_hash` covers sampling params (temperature/max_tokens/top_p) plus — for paraphrases — `paraphrase.PARAPHRASE_PROMPT_VERSION`.

**If you edit a completion/paraphrase prompt template, bump the relevant `*_PROMPT_VERSION` constant.** Editing without bumping leaves old rows keyed as if the prompt hadn't changed — they silently persist.

### Rumil-side judge versioning

Rumil-style judge_model strings (`rumil:ws:...`, `rumil:orch:...`, `rumil:text:...`) embed two version knobs:

- **`:p<hash>`** — automatic. `versus_bridge.compute_prompt_hash(task_body)` hashes `prompts/versus-judge-shell.md` + the task body (`prompts/versus-<name>.md` or a versus criterion prompt). Any `.md` edit forks the key.
- **`:v<N>`** — manual. `versus_bridge.BLIND_JUDGE_VERSION`. Bump when you make a semantic change the prompt hash doesn't catch. **Unhashed surfaces to watch:** `_format_pair_content`, the inline user prompts in `judge_pair_ws_aware` / `_run_orch_closer` / `_build_rumil_text_user_message`, the tool list / `disallowed_tools` config, `_versus_extra` contents. If you change any of those in a way that affects judge behavior, bump `BLIND_JUDGE_VERSION`.

### Sources, unified

Every "contestant" the judge sees is a row in `completions.jsonl`, with `source_kind ∈ {human, completion, paraphrase}` and a uniform `source_id`:
- `human` — the held-out remainder (written once per essay × prefix_config)
- `<model_id>` — from-scratch continuation by a completion model
- `paraphrase:<model_id>` — derived remainder of a model's full-essay paraphrase (synthesized from `paraphrases.jsonl` at completion-run time; no extra API call)

### Judging contract

Judges reason freely; we parse the **last** `<verdict>A|B|tie</verdict>` tag from the output (OpenRouter / anthropic:* variants) or the 7-point preference label (rumil:* variants). Don't constrain the whole response to JSON — chain-of-thought materially improves judgment quality.

Display order (A vs B) is deterministic per `(essay_id, sorted_pair)` via `judge.order_pair()` so every judge — model or human — sees the same assignment for the same pair.

### Blind judging

Source ids can literally be `"human"`, so any surface the judge sees (prompt, Question page headline, Question page content, `page.extra` which renders verbatim via `rumil.context.format_page`) must not disclose them. Test coverage is in `tests/test_versus_bridge.py`. Raw source ids stay in the judgment row for post-hoc analysis only.

### Running

```bash
cd versus
uv venv && uv pip install -e .
export OPENROUTER_API_KEY=...   # required for OpenRouter-based runs
export ANTHROPIC_API_KEY=...    # required for rumil-style judges

uv run scripts/fetch_essays.py
uv run scripts/run_paraphrases.py
uv run scripts/run_completions.py   # also synthesizes paraphrase-remainder rows
uv run scripts/run_judgments.py         # OpenRouter judges
uv run scripts/run_rumil_judgments.py   # Anthropic-direct / rumil-bridge judges
```

Env resolution for `ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY` cascades: `versus/.env`, then `<rumil-root>/.env`, then process env. Files override process env.

UI routes (`/versus`, `/versus/judge`, `/versus/inspect`, `/versus/results`) mount in the rumil Next.js frontend; API endpoints in `src/rumil/api/versus_router.py` read the JSONL stores directly. No DB tables.

### Rumil-style judge variants

`scripts/run_rumil_judgments.py` has four `--variant` options. See `.claude/skills/rumil-versus-judge/SKILL.md` for the detailed invocation guide, cost estimates, and confirmation thresholds.

- `text` — single-turn Anthropic call using versus's judge prompt. `judge_model = anthropic:<model>`.
- `rumil-text` — single-turn Anthropic call using rumil's dimension prompt (isolates prompt-source effect from workspace/tools effect). `judge_model = rumil:text:<model>:<dim>:p<hash>`.
- `ws` — one VERSUS_JUDGE agent call with workspace-exploration tools against a `--workspace`. `judge_model = rumil:ws:<model>:<ws>:<task>:p<hash>:v<N>`. Requires local Supabase.
- `orch` — full TwoPhaseOrchestrator run + closing call per pair. `judge_model = rumil:orch:<model>:<ws>:b<N>:<task>:p<hash>:v<N>`. Requires local Supabase. Expensive.

Model for ws/orch/rumil-text is passed explicitly through the bridge (`--rumil-model opus|sonnet|haiku`, default opus) — do not rely on `settings.model`. The bridge uses `override_settings(rumil_model_override=model)` to propagate to nested rumil calls.

### Known quirks

- Images not parsed from essay HTML; screen-reader "Image" labels filtered explicitly.
- Forethought essays end with a `Footnotes` heading + acknowledgement paragraph; both stripped at fetch time.
- Length tolerance is a prompt hint, not a hard constraint. Some models consistently undershoot — real signal, not to silently correct.
- `fetch.SCHEMA_VERSION` invalidates the essay JSON cache. Raw HTML stays cached separately so we don't re-download.
- Refused / content-filtered completions are excluded from pair enumeration (see `judge.is_refusal`).

## Key Conventions

- **NEVER pass `--prod` when running `main.py` unless the user explicitly asks you to.** The production database contains real research data. Default to the local database for all testing, development, and exploratory runs.
- **Never run `supabase db reset`** — this wipes the database and is destructive. To apply pending migrations, use `supabase migration up` instead. If you find yourself wanting to reset the database, stop and ask the user first.
- Always scope your test runs to a temp/scratch workspace, e.g. `uv run main.py "Is the sky blue?" --workspace skyblue-scratch`

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
