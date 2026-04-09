# Rumil

## Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** — Python package manager
- **[Docker Desktop](https://www.docker.com/products/docker-desktop/)** — required by Supabase CLI
- **[Supabase CLI](https://supabase.com/docs/guides/local-development/cli/getting-started#installing-the-supabase-cli)** — manages the local Postgres instance
- **Anthropic API key** with credits on it

### Installing Supabase CLI

```bash
# macOS (Homebrew)
brew install supabase/tap/supabase

# pnpm
pnpm add -g supabase

# Or see https://supabase.com/docs/guides/local-development/cli/getting-started
```

## Setup

```bash
# 1. Install Python dependencies
uv sync

# 2. Start the local Supabase stack (Postgres, PostgREST, etc.)
#    First run will pull Docker images — may take a few minutes.
supabase start

# 3. Set your Anthropic API key
export ANTHROPIC_API_KEY="sk-ant-..."
#    Or create a .env file in the repo root:
#    ANTHROPIC_API_KEY=sk-ant-...
```

`supabase start` runs migrations automatically, so the database is ready to use immediately.

### Production database

To run against the production Supabase instance, add these to your `.env`:

```
SUPABASE_PROD_URL=https://aesjaehibxrzearctiqp.supabase.co
SUPABASE_PROD_KEY=<service_role key from Supabase dashboard>
```

Then pass `--prod` to any command. Without it, all commands target the local database.

To push migrations to production:

```bash
supabase link --project-ref <project-ref>
supabase db push
```

## Usage

```bash
# New investigation
uv run python main.py "Your question here" --budget 20

# Continue an existing question with more budget
uv run python main.py --continue QUESTION_ID --budget 10

# Add a question without investigating it yet
uv run python main.py --add-question "Some sub-question" --budget 0

# List all questions
uv run python main.py --list

# Ingest a source document
uv run python main.py --ingest FILE --for-question QUESTION_ID --budget 5

# Interactive chat about research
uv run python main.py --chat QUESTION_ID

# Generate HTML research map
uv run python main.py --map QUESTION_ID

# Generate executive summary
uv run python main.py --summary QUESTION_ID

# Generate a multi-section research report
uv run python main.py --report QUESTION_ID

# Run a concept-generation session (propose and assess conceptual tools for the research)
uv run python main.py --concepts QUESTION_ID

# Evaluate the judgement quality for a question
uv run python main.py --evaluate QUESTION_ID

# Use a specific evaluation prompt type (default: "default")
uv run python main.py --evaluate QUESTION_ID --eval-type grounding

# Find existing questions in the workspace that should be linked as subquestions
# of a scope question. Returns proposed ids without creating links.
uv run python main.py --link-subquestions QUESTION_ID

# Override the linker agent's max exploration rounds (default: 6)
uv run python main.py --link-subquestions QUESTION_ID --linker-max-rounds 4

# Display the full output of a completed evaluation
uv run python main.py --show-evaluation CALL_ID

# Run grounding feedback on a completed evaluation (improves workspace sourcing)
uv run python main.py --ground EVAL_CALL_ID

# Control how deep the summary traverses and where it switches to compact mode.
# --max-depth N        How many levels of sub-questions to include (default: 4).
# --summarize-after-depth N  Levels 0..N-1 show full claim/judgement content;
#                            deeper levels show only one-line page summaries.
#                            Default: max-depth // 2. Decrease to shrink context
#                            when hitting LLM context-length errors.
uv run python main.py --summary QUESTION_ID --max-depth 6 --summarize-after-depth 3

# Batch mode: investigate multiple questions concurrently
uv run python main.py --batch questions.json

# Use a named workspace to isolate investigations
uv run python main.py "Your question here" --workspace my-project --budget 10

# List all workspaces
uv run python main.py --list-workspaces

# List questions in a specific workspace
uv run python main.py --list --workspace my-project

# A/B test: run two arms concurrently with different configs
# Requires .a.env and .b.env files with differing settings
uv run python main.py --ab "Your question here" --budget 10

# Name a run for easier identification in the trace viewer
uv run python main.py "Your question here" --name "baseline v2" --budget 10

# Retroactively stage a completed run (hides its effects from baseline readers)
uv run python main.py --stage-run RUN_ID

# Commit a staged run (makes its effects visible to all readers)
uv run python main.py --commit-run RUN_ID

# Smoke-test mode: uses Haiku, fewer agent rounds, budget defaults to 1
uv run python main.py "Your question here" --smoke-test

# Any command can target the production database
uv run python main.py --prod --list

# Select an available-moves preset (controls which tools are available per call type)
uv run python main.py "Your question" --available-moves default --budget 10

# Select an available-calls preset (controls which scouts/dispatches the two-phase orchestrator uses)
# 'default' = standard scouts, 'multi-subquestion' = replaces generic subquestions scout with web-questions and deep-questions scouts
uv run python main.py "Your question" --available-calls multi-subquestion --budget 20

# Suppress info-level logging (only warnings and errors)
uv run python main.py "Your question" --budget 5 -q

# Enable debug-level logging to stderr (very verbose)
uv run python main.py "Your question" --budget 5 --debug
```

### Batch mode

The `--batch` flag accepts a JSON file containing an array of questions to investigate concurrently. Each question runs with its own budget in parallel via `asyncio.gather`.

```json
[
  {"question": "What causes coral reef bleaching?", "budget": 10},
  {"question": "How does sleep deprivation affect cognition?", "budget": 5},
  {"continue": "323d2d09-3463-434d-8541-68df5aaaa148", "budget": 10}
]
```

Each entry supports:

| Field | Required | Description |
|-------|----------|-------------|
| `question` | One of `question` or `continue` | New question to investigate |
| `continue` | One of `question` or `continue` | ID of an existing question to continue |
| `budget` | No (default: 10) | Research call budget for this entry |
| `ingest` | No | List of file paths to ingest (only with `question`) |

For PDF ingestion, install the optional dependency: `uv sync --extra pdf`

### A/B testing

The `--ab` flag runs two concurrent investigations of the same question with different configurations. Each arm reads its settings from a separate env file (`.a.env` and `.b.env`), allowing you to compare call variants, context budgets, or other settings side by side.

```bash
# Create arm-specific config files
echo 'SCOUT_CALL_VARIANT=default' > .a.env
echo 'SCOUT_CALL_VARIANT=embedding' > .b.env

# Run the AB test
uv run python main.py --ab "Your question" --budget 10 --workspace ab-scratch

# View results in the frontend at /ab-traces/{ab_run_id}
```

Pages created by each arm are isolated — arm A cannot see pages created by arm B, and vice versa. Shared pages (like the root question) are visible to both arms. The frontend shows a side-by-side trace comparison with config diff highlighting.

### Testing individual calls

`scripts/run_call.py` runs a single call type (find-considerations, assess, prioritize) against the local database, useful for development and debugging without the full orchestrator loop.

```bash
# Find considerations on a new question
uv run python scripts/run_call.py find-considerations "Is the sky blue?"

# Find considerations on an existing question by ID
uv run python scripts/run_call.py find-considerations --question-id <UUID>

# Assess or prioritize an existing question
uv run python scripts/run_call.py assess --question-id <UUID>
uv run python scripts/run_call.py prioritize --question-id <UUID> --budget 5

# Override find-considerations params
uv run python scripts/run_call.py find-considerations "Why is water wet?" --mode concrete --max-rounds 3

# Use a custom workspace (default: test-calls)
uv run python scripts/run_call.py find-considerations "Test question" --workspace my-scratch

# Use smoke-test mode
uv run python scripts/run_call.py find-considerations "Test question" --smoke-test

# Stop after a specific stage (build_context or create_pages)
uv run python scripts/run_call.py find-considerations "Test question" --up-to-stage build_context
uv run python scripts/run_call.py find-considerations "Test question" --up-to-stage create_pages

# A/B test a single call (requires .a.env and .b.env)
uv run python scripts/run_call.py find-considerations "Test question" --ab --smoke-test

# Name a run for easier identification
uv run python scripts/run_call.py find-considerations "Test question" --name "context experiment"
```

The `--up-to-stage` flag truncates the call lifecycle. Each call runs three stages in order: `build_context` → `create_pages` → `closing_review`. Passing `--up-to-stage build_context` runs only context assembly; `--up-to-stage create_pages` skips the closing review. Useful for inspecting context or page output in isolation.

The `--ab` flag works the same as in `main.py` — it runs both arms concurrently with settings from `.a.env` and `.b.env`, and prints an AB trace URL.

## Frontend

The frontend is a Next.js app that reads from a FastAPI server.

```bash
# Start the API server (requires local Supabase to be running)
uv run uvicorn rumil.api.app:app --reload

# Start the frontend dev server
cd frontend && pnpm dev
```

To point the API at the production database, set `USE_PROD_DB=1` before starting uvicorn.

## Tests

```bash
uv run pytest

```

## Supabase Studio

While `supabase start` is running, you can browse your data at [http://127.0.0.1:54323](http://127.0.0.1:54323).
