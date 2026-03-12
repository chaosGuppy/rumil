# Differential

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
SUPABASE_PROD_URL=https://<project-ref>.supabase.co
SUPABASE_PROD_KEY=<service_role key from Supabase dashboard>
```

Then pass `--prod-db` to any command. Without it, all commands target the local database.

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

# Smoke-test mode: uses Haiku, fewer agent rounds, budget defaults to 1
uv run python main.py "Your question here" --smoke-test

# Any command can target the production database
uv run python main.py --prod-db --list

# Enable info-level logging to stderr
uv run python main.py "Your question" --budget 5 -v

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

## Frontend

The frontend is a Next.js app that reads from a FastAPI server.

```bash
# Start the API server (requires local Supabase to be running)
uv run uvicorn differential.api.app:app --reload

# Start the frontend dev server
cd frontend && pnpm dev
```

To point the API at the production database, set `DIFFERENTIAL_PROD_DB=1` before starting uvicorn.

## Tests

```bash
uv run pytest

```

## Supabase Studio

While `supabase start` is running, you can browse your data at [http://127.0.0.1:54323](http://127.0.0.1:54323).
