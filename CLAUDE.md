## What This Is

An LLM-powered research workspace. Users pose questions, and the system recursively investigates them by making structured LLM calls (scout, assess, prioritize, ingest) that produce "pages" (claims, questions, judgements, concepts) stored in a SQLite database. Pages link together into a research tree with considerations bearing on questions.

## Running

Requires `ANTHROPIC_API_KEY` in environment. Uses `anthropic` Python SDK and `claude-opus-4-6`. Environment managed with `uv`.

```bash
# New investigation
uv run python main.py "Your question here" --budget 20

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
```

Tests: `uv run pytest`. Optional dependency: `pypdf` for PDF ingestion (`uv sync --extra pdf`).

## Architecture

**Entry point:** `main.py` — CLI arg parsing, dispatches to command functions.

**Package:** `src/differential/` — installed as `differential` via hatch/uv. Uses src layout. Always use absolute imports (e.g. `from differential.database import DB`).

**Core loop** (`src/differential/orchestrator.py`): `Orchestrator.run()` → `investigate_question()` recursively. Runs a free prioritization call to plan budget allocation, then dispatches scout/assess/sub-prioritization calls. Budget is a global counter in the DB; each scout/assess/ingest call costs 1 unit. Prioritization and closing reviews are free.

**Call types** (`src/differential/calls/`): Package with one module per call type (`scout.py`, `assess.py`, `prioritization.py`, `ingest.py`) plus `common.py` for shared utilities (phase management, closing reviews, page loading). Public API re-exported from `__init__.py`. Scout, Assess, and Ingest use a two-phase pattern:
- Phase 1 (free): LLM sees workspace map + working context, can request pages via LOAD_PAGE, writes planning notes
- Phase 2 (costs 1 budget): continues conversation with loaded pages, does real work. Supports iterative LOAD_PAGE within phase 2 (up to 3 rounds).

Each call ends with a closing review that produces `remaining_fruit` (0-10 scale) — the orchestrator uses this to decide when to stop scouting.

**LLM interface** (`src/differential/llm.py`): Wraps the Anthropic API. `run_call()` builds prompts from `prompts/` directory: system = preamble.md + call-type-specific .md file, user = context + task. Has retry logic for transient errors.

**Prompt structure** (`prompts/`): `preamble.md` defines the workspace model, page types, move format, and epistemic conventions shared across all call types. Each call type has its own prompt file (scout.md, assess.md, prioritization.md, ingest.md, etc.).

**Output parsing** (`src/differential/parser.py`): LLM output is XML-style `<move type="...">` tags containing JSON payloads. Also parses `<dispatch>` tags (from prioritization) and `<review>` tags (from closing reviews).

**Move execution** (`src/differential/executor.py`): Takes parsed moves and writes pages/links to DB + markdown files to `pages/`. Supports `LAST_CREATED` placeholder for linking immediately after creation. Move types: CREATE_CLAIM, CREATE_QUESTION, CREATE_JUDGEMENT, CREATE_CONCEPT, LINK_CONSIDERATION, LINK_CHILD_QUESTION, SUPERSEDE_PAGE, FLAG_FUNNINESS, REPORT_DUPLICATE, LOAD_PAGE.

**Data layer** (`src/differential/database.py`): SQLite with WAL mode. Tables: pages, page_links, calls, budget, page_ratings, page_flags. `DB` class opens a new connection per method call. Has auto-migration for schema changes.

**Data models** (`src/differential/models.py`): Dataclasses for Page, PageLink, Call. Key enums: PageType (source/claim/question/judgement/concept/wiki), CallType (scout/assess/prioritization/ingest/reframe/maintain), LinkType (consideration/child_question/supersedes/related), ConsiderationDirection (supports/opposes/neutral).

**Context building** (`src/differential/context.py`): Assembles LLM context from DB state. `build_call_context()` prepends a compact workspace map (from `src/differential/workspace_map.py`) then detailed working context for the target question. `build_prioritization_context()` includes a question index with dispatchable IDs.

**Outputs:**
- `db/workspace.db` — SQLite database (gitignored)
- `pages/research/` — markdown files per page
- `pages/maps/` — HTML research maps
- `pages/summaries/` — generated summaries

## Key Conventions

- Epistemic status is a 0-5 float (subjective confidence), always paired with an epistemic_type string
- Consideration strength is 0-5 (relevance to question)
- Page summaries must be 10-15 words, self-contained headlines
- Short IDs are first 8 chars of UUID, used in workspace maps and display
- Always use absolute imports: `from differential.module import name` (no relative imports)
- Use modern type syntax: `X | None` not `Optional[X]`, `list[str]` not `List[str]`, etc. No `from typing import Optional, List, Dict`.
- Pages are immutable once written (squidgy layer); updates use SUPERSEDE_PAGE to create a new version pointing back to the old one
- Multiline strings use parenthesized concatenation of single-quoted lines (`"line 1 " "line 2"`), not triple-quoted strings (`"""`). Only use `f""` on lines that actually contain `{placeholder}` expressions.
