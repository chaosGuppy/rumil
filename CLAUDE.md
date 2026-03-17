## What This Is

An LLM-powered research workspace. Users pose questions, and the system recursively investigates them by making structured LLM calls (scout, assess, prioritize, ingest) that produce "pages" (claims, questions, judgements, concepts) stored in a Supabase (Postgres) database. Pages link together into a research tree with considerations bearing on questions.

## Running

Requires `ANTHROPIC_API_KEY` in environment. Uses `anthropic` Python SDK and `claude-opus-4-6`. Environment managed with `uv`. Data is stored in Supabase — local by default, or production with `--prod`.

```bash
# New investigation
uv run python main.py "Your question here" --budget 20

# Use production database (any command)
uv run python main.py --prod "Your question here" --budget 20

# Continue existing question
uv run python main.py --continue QUESTION_ID --budget 10

# Ingest a source document
uv run python main.py --ingest FILE --for-question QUESTION_ID --budget 5

# List questions
uv run python main.py --list

# Interactive chat about research
uv run python main.py --chat QUESTION_ID

# Generate HTML research map
uv run python main.py --map QUESTION_ID

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

Tests: `uv run pytest`. Optional dependency: `pypdf` for PDF ingestion (`uv sync --extra pdf`).

**Database:** Runs against local Supabase by default (`supabase start`). Pass `--prod` to any command to target production. Production requires `SUPABASE_PROD_URL` and `SUPABASE_PROD_KEY` (service_role) in `.env`. Migrations live in `supabase/migrations/` and are pushed to prod with `supabase db push`.
Always use the supabase cli to create new migrations: `supabase migration new`.

## Architecture

**Entry point:** `main.py` — CLI arg parsing, dispatches to command functions.

**Package:** `src/rumil/` — installed as `rumil` via hatch/uv. Uses src layout. Always use absolute imports (e.g. `from rumil.database import DB`).

**Core loop** (`src/rumil/orchestrator.py`): `Orchestrator.run()` → `investigate_question()` recursively. Runs a free prioritization call to plan budget allocation, then dispatches scout/assess/sub-prioritization calls via call variant registries. Budget is a global counter in the DB; each scout/assess/ingest call costs 1 unit. Prioritization and closing reviews are free.

**Call types** (`src/rumil/calls/`): Polymorphic class hierarchy using a template method pattern. `BaseCall` defines `run()` which orchestrates three abstract phases: `build_context()`, `create_pages()`, `closing_review()`. Each call type lives in its own module (`scout.py`, `assess.py`, `prioritization.py`, `ingest.py`). `common.py` has shared utilities (`run_agent_loop()`, `run_single_call()`, closing reviews, dispatch tool). Public API re-exported from `__init__.py`.

Class hierarchy:
- `BaseCall` (abstract) — owns `run()` orchestration, `CallTrace`, `MoveState`, shared finalization. Subclasses implement the three phase methods plus `result_summary()`.
- `SimpleCall(BaseCall)` — concrete `create_pages()` (single `run_agent_loop` pass) and `closing_review()` (structured review producing `remaining_fruit`, `confidence_in_output`, `page_ratings`). Also provides `_load_phase1_pages()` for a free preliminary LLM call where the model can only `load_page` to preload relevant pages into context. Subclasses override `build_context()`, `call_type()`, `task_description()`, and `result_summary()`.
- `AssessCall(SimpleCall)` / `IngestCall(SimpleCall)` — minimal overrides; build context via `build_call_context()`, then call `_load_phase1_pages()`.
- `ScoutCall(BaseCall)` — implements all three phases directly. Multi-round `create_pages()` with conversation resumption and fruit-check stopping. Two-phase `closing_review()` (link review via `run_session_review()` → `_self_assessment()`).
- Embedding variants (`EmbeddingAssessCall`, `EmbeddingIngestCall`, `EmbeddingScoutCall`) — subclass the above, override `build_context()` to use `build_embedding_based_context()` as the sole context source. `EmbeddingScoutCall` also skips `link_new_pages()` and the link review phase in closing (overrides `run_session_review()` to go straight to `_self_assessment()`).

The three phases:
1. **build_context** (free): Assemble `self.context_text` from workspace map + working context. Optionally run `_load_phase1_pages()` (free LLM call with only `load_page` tool) to preload pages into context. Record `ContextBuiltEvent`.
2. **create_pages** (costs budget): Main agent loop with move tools. `SimpleCall` runs one `run_agent_loop` pass. `ScoutCall` runs multiple rounds with fruit checking between them, resuming the conversation across rounds.
3. **closing_review** (free): Self-assessment producing `remaining_fruit` (0-10) — the orchestrator uses this to decide when to stop scouting. Also produces `confidence_in_output`, `context_was_adequate`, and `page_ratings`.

To add a new call type: subclass `SimpleCall` (for single-pass calls) or `BaseCall` (for custom loop logic). Implement `build_context()`, `call_type()`, `task_description()`, `result_summary()`. Register the class in the appropriate registry in `call_registry.py` and export from `__init__.py`.

**Call variant registries** (`src/rumil/calls/call_registry.py`): Each call type (scout, assess, ingest) has a registry dict mapping string names to concrete classes (e.g. `SCOUT_CALL_CLASSES = {"default": ScoutCall, "embedding": EmbeddingScoutCall}`). The orchestrator looks up the active variant from settings (`scout_call_variant`, `assess_call_variant`, `ingest_call_variant`) and instantiates directly — there are no wrapper functions.

**LLM interface** (`src/rumil/llm.py`): Wraps the Anthropic API. Provides `call_api()` (single API call with tool handling), `structured_call()` (structured output), and `text_call()`. The multi-turn agent loop lives in `calls/common.py` (`run_agent_loop()`). For single-turn tool-calling use `run_single_call()` — do NOT use `run_agent_loop` with `max_rounds=1`. Both support `messages` for conversation resumption and `cache=True` for prompt caching. When multiple LLM calls share a conversation prefix, pass the same tools to all of them (even if the prompt only asks the model to use a subset) so the cache prefix matches. Builds prompts from `prompts/` directory: system = preamble.md + call-type-specific .md file, user = context + task. Has retry logic for transient errors.

**Prompt structure** (`prompts/`): `preamble.md` defines the workspace model, page types, and epistemic conventions shared across all call types. Each call type has its own prompt file (scout.md, assess.md, prioritization.md, ingest.md, etc.).

**Moves** (`src/rumil/moves/`): Package with one module per move type. Each module defines a pydantic payload schema, an `execute()` function, and a `MoveDef` that binds them together as a tool. `base.py` has shared helpers (page creation, linking, `LAST_CREATED` resolution). `registry.py` collects all moves into a `MOVES` dict keyed by `MoveType`. See `MoveType` enum in `models.py` for the full list.

**Data layer** (`src/rumil/database.py`): Supabase (Postgres) via the `supabase` Python SDK. Tables: pages, page_links, calls, budget, page_ratings, page_flags. `DB.create(run_id, prod=True)` classmethod handles connection setup (delegated to `settings.py`); defaults to local Supabase. Several operations use Postgres RPC functions defined in the migrations.

**Data models** (`src/rumil/models.py`): Pydantic BaseModels for Page, PageLink, Call, Project. Used directly as both internal models and FastAPI response types (no separate `*Out` duplicates). Fields with defaults use `_all_fields_required` schema helper so they appear required in the OpenAPI spec. Key enums: PageType (source/claim/question/judgement/concept/wiki), CallType (scout/assess/prioritization/ingest/reframe/maintain), LinkType (consideration/child_question/supersedes/related), ConsiderationDirection (supports/opposes/neutral), MoveType (the full set of moves the LLM can emit). MoveType is the source of truth for valid moves — the moves registry maps each to its `MoveDef`. `DISPATCHABLE_CALL_TYPES` defines which `CallType`s prioritization can dispatch (scout/assess/prioritization) — the dispatch tool validates against it and the orchestrator dispatches on `CallType` enum values.

**Context building** (`src/rumil/context.py`): Assembles LLM context from DB state. `build_call_context()` prepends a compact workspace map (from `src/rumil/workspace_map.py`) then detailed working context for the target question. `build_prioritization_context()` includes a question index with dispatchable IDs. `build_embedding_based_context()` uses vector similarity search (`embed_query` + `search_pages_by_vector`) to surface the most relevant pages from the entire workspace regardless of graph distance, filling a full-page tier then a summary tier within configurable char budgets (settings: `context_char_budget`, `full_page_char_fraction`, `summary_page_char_fraction`, `distillation_page_char_fraction`).

**Tracing** (`src/rumil/tracer.py`, `src/rumil/trace_events.py`): `CallTrace` accumulates typed events during a call's lifecycle and persists them as JSONB in the `trace_json` column on `calls`. Events are defined as Pydantic models in `trace_events.py` with a `Literal` discriminator field (e.g. `event: Literal["context_built"] = "context_built"`). The `TraceEvent` discriminated union is the accepted type for `CallTrace.record()`. API envelope types in `schemas.py` inherit from these events and add `ts`/`call_id` fields. Frontend types are auto-generated. To add a new trace event: (1) define a new Pydantic model in `trace_events.py` with an `event: Literal["..."] = "..."` field, (2) add it to the `TraceEvent` union, (3) create a corresponding `*EventOut` subclass in `schemas.py` that inherits from both the event and `_TraceEnvelopeMixin`, (4) add it to the `TraceEventOut` union, (5) regenerate frontend types with `./scripts/generate-api-types.sh`, (6) handle the new event in the frontend's `EventSection` component in `call-node.tsx`.

**API** (`src/rumil/api/`): FastAPI read-only API for the frontend. Core models from `models.py` are used directly as response types. `schemas.py` defines composite response types (e.g. `CallTraceOut`) and trace event envelope types. `app.py` defines endpoints. Run with `uv run uvicorn rumil.api.app:app --reload`.

**Frontend** (`frontend/`): Next.js TypeScript app with Tailwind. Uses pnpm. Run with `cd frontend && pnpm dev`. TypeScript types in `frontend/src/api/` are auto-generated from the API's OpenAPI schema — **never create or edit these files by hand**. When API schemas change, regenerate with `./scripts/generate-api-types.sh` (or `cd frontend && pnpm generate-api`). This is the only mechanism for sharing types between backend and frontend; do not manually duplicate type definitions.

**Outputs:**
- `pages/research/` — markdown files per page
- `pages/maps/` — HTML research maps
- `pages/summaries/` — generated summaries

## Key Conventions

- **NEVER pass `--prod` when running `main.py` unless the user explicitly asks you to.** The production database contains real research data. Default to the local database for all testing, development, and exploratory runs.
- **Never run `supabase db reset`** — this wipes the database and is destructive. To apply pending migrations, use `supabase migration up` instead. If you find yourself wanting to reset the database, stop and ask the user first.
- Always scope your test runs to a temp/scratch workspace, e.g. `uv run main.py "Is the sky blue?" --workspace skyblue-scratch`

- Epistemic status is a 0-5 float (subjective confidence), always paired with an epistemic_type string
- Consideration strength is 0-5 (relevance to question)
- Page summaries must be 10-15 words, self-contained headlines
- Short IDs are first 8 chars of UUID, used in workspace maps and display
- Always use absolute imports: `from rumil.module import name` (no relative imports)
- Always put imports at the top of the file, not inside functions
- Use modern type syntax: `X | None` not `Optional[X]`, `list[str]` not `List[str]`, etc. No `from typing import Optional, List, Dict`.
- Prefer `Sequence` over `list` in type hints for parameters and return types. Only use `list` where mutation (e.g. `.append()`) is actually needed.
- Pages are immutable once written (squidgy layer); updates use SUPERSEDE_PAGE to create a new version pointing back to the old one
- Multiline strings use parenthesized concatenation of single-quoted lines (`"line 1 " "line 2"`), not triple-quoted strings (`"""`). Only use `f""` on lines that actually contain `{placeholder}` expressions.
- Do not add section divider comments (e.g. `# ----------` banners). Use blank lines between logical sections; the code should speak for itself.
- When adding new user-facing CLI flags or commands to `main.py`, always update `README.md` with corresponding documentation.
