## What This Is

An LLM-powered research workspace. Users pose questions, and the system investigates them by making structured LLM calls (find_considerations, assess, prioritize, ingest) that produce "pages" (claims, questions, judgements, concepts). Pages link together into a research graph with considerations bearing on questions. The codebase is optimised for experimentation, containing multiple implementations of pluggable abstractions, rather than being a monolithic application where there's only one way of achieving things.

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

## Architecture

**Entry point:** `main.py` — CLI arg parsing, dispatches to command functions.

**Single-call runner** (`scripts/run_call.py`): Runs one call type against the local database. Supports `--workspace`, `--smoke-test`, `--ab` (A/B testing with `.a.env`/`.b.env`), `--name`, and `--up-to-stage` (truncate the call lifecycle after `build_context` or `create_pages`). Runs are recorded in the `runs` table with captured config.

**Package:** `src/rumil/` — installed as `rumil` via hatch/uv. Uses src layout. Always use absolute imports (e.g. `from rumil.database import DB`).

**Orchestrators** (`src/rumil/orchestrator.py`): Orchestrators determine the sequence of calls to dispatch. Each represents a different way of prioritizing and executing research. 

**Call types** (`src/rumil/calls/`): Composition-based architecture using three pluggable stage ABCs. `CallRunner` (in `stages.py`) orchestrates the three phases by delegating to `ContextBuilder`, `PageCreator`, and `ClosingReviewer` instances. Each call type lives in its own module (`find_considerations.py`, `assess.py`, `ingest.py`, etc.) as a thin `CallRunner` subclass. `common.py` has shared utilities (`run_agent_loop()`, `run_single_call()`, closing reviews). Public API re-exported from `__init__.py`.

Architecture:
- `CallRunner` (`stages.py`) — base class for all call types. Owns `run()` orchestration via `CallInfra` (bundles `CallTrace`, `MoveState`, DB, call). Subclasses set class-level `context_builder_cls`, `page_creator_cls`, `closing_reviewer_cls`, and `call_type` attributes, plus override `_make_*()` factory methods for parameterized stages and `task_description()`.
- `ContextBuilder` ABC — `build_context(infra) -> ContextResult`. Implementations in `context_builders.py`: `GraphContextWithPhase1`, `EmbeddingContext`, `IngestGraphContext`, `IngestEmbeddingContext`, `FindConsiderationsGraphContext`, `ScoutEmbeddingContext`, `ConceptScoutContext`, `ConceptAssessContext`, `WebResearchEmbeddingContext`.
- `PageCreator` ABC — `create_pages(infra, context) -> CreationResult`. Implementations in `page_creators.py`: `SimpleAgentLoop` (single-pass), `MultiRoundLoop` (multi-round with fruit checks), `WebResearchLoop` (server tools + scraping).
- `ClosingReviewer` ABC — `closing_review(infra, context, creation) -> None` (persists all results as side effects). Implementations in `closing_reviewers.py`: `StandardClosingReview`, `IngestClosingReview`, `WebResearchClosingReview`, `TwoPhaseScoutReview`, `SinglePhaseScoutReview`, `ConceptAssessReview`.
- Data types (`stages.py`): `CallInfra` (shared infra), `ContextResult` (context output), `CreationResult` (page creation output).

The three phases:
1. **build_context** — `ContextBuilder.build_context()` returns `ContextResult`
2. **create_pages** — `PageCreator.create_pages()` returns `CreationResult`
3. **closing_review** — `ClosingReviewer.closing_review()` persists results and calls `mark_call_completed()`

To add a new call type: subclass `CallRunner`. Set `call_type`, override `_make_context_builder()`, `_make_page_creator()`, `_make_closing_reviewer()`, and `task_description()`. For simple calls, reuse `SimpleAgentLoop` + `StandardClosingReview` + an existing context builder. Register the class in `call_registry.py` and export from `__init__.py`.

**Call variant registries** (`src/rumil/calls/call_registry.py`): Each call type (find_considerations, assess, ingest) has a registry dict mapping string names to concrete classes (e.g. `FIND_CONSIDERATIONS_CALL_CLASSES = {"default": FindConsiderationsCall, "embedding": EmbeddingFindConsiderationsCall}`). The orchestrator looks up the active variant from settings (`find_considerations_call_variant`, `assess_call_variant`, `ingest_call_variant`) and instantiates directly.

**Move presets** (`src/rumil/move_presets.py`): Named mappings from `CallType` to `Sequence[MoveType]`, controlling which tools each call type can use. `PRESETS` dict holds all presets; `get_moves_for_call()` reads the active preset from `settings.move_preset`. Call types absent from a preset get all moves. `CallRunner._resolve_available_moves()` checks the preset first, then falls back to the class-level `available_moves`. CLI flag: `--moves-preset`.

**LLM interface** (`src/rumil/llm.py`): Wraps the Anthropic API. Provides `call_api()` (single API call with tool handling), `structured_call()` (structured output), and `text_call()`. The multi-turn agent loop lives in `calls/common.py` (`run_agent_loop()`). For single-turn tool-calling use `run_single_call()` — do NOT use `run_agent_loop` with `max_rounds=1`. Both support `messages` for conversation resumption and `cache=True` for prompt caching. When multiple LLM calls share a conversation prefix, pass the same tools to all of them (even if the prompt only asks the model to use a subset) so the cache prefix matches. Builds prompts from `prompts/` directory: system = preamble.md + call-type-specific .md file, user = context + task. Has retry logic for transient errors.

**Prompt structure** (`prompts/`): `preamble.md` defines the workspace model, page types, and epistemic conventions shared across all call types. Each call type has its own prompt file (find_considerations.md, assess.md, prioritization.md, ingest.md, etc.).

**Moves** (`src/rumil/moves/`): Package with one module per move type. Each module defines a pydantic payload schema, an `execute()` function, and a `MoveDef` that binds them together as a tool. `base.py` has shared helpers (page creation, linking, `LAST_CREATED` resolution). `registry.py` collects all moves into a `MOVES` dict keyed by `MoveType`. See `MoveType` enum in `models.py` for the full list.

**Data layer** (`src/rumil/database.py`): Supabase (Postgres) via the `supabase` Python SDK. Tables: pages, page_links, calls, budget, page_ratings, page_flags. `DB.create(run_id, prod=True)` classmethod handles connection setup (delegated to `settings.py`); defaults to local Supabase. Several operations use Postgres RPC functions defined in the migrations.

**Data models** (`src/rumil/models.py`): Pydantic BaseModels for Page, PageLink, Call, Project. Used directly as both internal models and FastAPI response types (no separate `*Out` duplicates). Fields with defaults use `_all_fields_required` schema helper so they appear required in the OpenAPI spec. Key enums: PageType, CallType, CallStage, LinkType, ConsiderationDirection, MoveType. MoveType is the source of truth for valid moves — the moves registry maps each to its `MoveDef`. `DISPATCHABLE_CALL_TYPES` defines which `CallType`s prioritization can dispatch (find_considerations/assess/prioritization) — the dispatch tool validates against it and the orchestrator dispatches on `CallType` enum values.

**Context building** (`src/rumil/context.py`): Assembles LLM context from DB state. `build_call_context()` prepends a compact workspace map (from `src/rumil/workspace_map.py`) then detailed working context for the target question. `build_prioritization_context()` includes a question index with dispatchable IDs. `build_embedding_based_context()` uses vector similarity search (`embed_query` + `search_pages_by_vector`) to surface the most relevant pages from the entire workspace regardless of graph distance, filling a full-page tier then a summary tier within configurable char budgets (settings: `context_char_budget`, `full_page_char_fraction`, `summary_page_char_fraction`, `distillation_page_char_fraction`).

**Tracing** (`src/rumil/tracer.py`, `src/rumil/trace_events.py`): `CallTrace` accumulates typed events during a call's lifecycle and persists them as JSONB in the `trace_json` column on `calls`. Events are defined as Pydantic models in `trace_events.py` with a `Literal` discriminator field (e.g. `event: Literal["context_built"] = "context_built"`). The `TraceEvent` discriminated union is the accepted type for `CallTrace.record()`. API envelope types in `schemas.py` inherit from these events and add `ts`/`call_id` fields. Frontend types are auto-generated. To add a new trace event: (1) define a new Pydantic model in `trace_events.py` with an `event: Literal["..."] = "..."` field, (2) add it to the `TraceEvent` union, (3) create a corresponding `*EventOut` subclass in `schemas.py` that inherits from both the event and `_TraceEnvelopeMixin`, (4) add it to the `TraceEventOut` union, (5) regenerate frontend types with `./scripts/generate-api-types.sh`, (6) handle the new event in the frontend's `EventSection` component in `call-node.tsx`.

**API** (`src/rumil/api/`): FastAPI read-only API for the frontend. Core models from `models.py` are used directly as response types. `schemas.py` defines composite response types (e.g. `CallTraceOut`) and trace event envelope types. `app.py` defines endpoints. Run with `./scripts/dev-api.sh` — this reads the API port from `frontend/.env.local` so it matches what the frontend expects. To stop the server, read the port from `frontend/.env.local` (`NEXT_PUBLIC_API_URL`) and kill the process on that port.

**Frontend** (`frontend/`): Next.js TypeScript app with Tailwind. Uses pnpm. Run with `cd frontend && pnpm dev`. The frontend port mirrors the API port: if the API is on `localhost:800X`, the frontend will be on `localhost:300X` (Next.js auto-increments when the default port is taken). Use this to find and stop the frontend process. TypeScript types in `frontend/src/api/` are auto-generated from the API's OpenAPI schema — **never create or edit these files by hand**. When API schemas change, theese need to be regenerated with `./scripts/generate-api-types.sh` (or `cd frontend && pnpm generate-api`). This is the only mechanism for sharing types between backend and frontend; do not manually duplicate type definitions. When `schemas.py` or `models.py` is edited, `./scripts/generate-api-types.sh` is automaticaly run via a hook.

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