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

# Generate executive summary
uv run python main.py --summary QUESTION_ID

# Investigate and summarize in one command
uv run python main.py "Your question here" --budget 20 --summary

# Generate a multi-section research report
uv run python main.py --report QUESTION_ID

# Self-improvement: analyse how an investigation went and get rumil
# code/prompt improvement suggestions. Read-only; inspects the run plus
# rumil's own source via LLM tools, then writes a markdown analysis to
# pages/self-improvement/.
uv run python main.py --self-improve QUESTION_ID

# Investigate and self-improve in one command (analyses the just-finished run)
uv run python main.py "Your question here" --budget 20 --self-improve

# Evaluate the judgement quality for a question
uv run python main.py --evaluate QUESTION_ID

# Use a specific evaluation prompt type (default: "default")
uv run python main.py --evaluate QUESTION_ID --eval-type grounding

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

# Export workspace as an Obsidian vault
uv run python main.py --obsidian ./vault --workspace my-project

# Export a single question's subtree as an Obsidian vault
uv run python main.py --obsidian ./vault QUESTION_ID

# Investigate and export to Obsidian in one command
uv run python main.py "Your question here" --budget 20 --obsidian ./vault

# Use a named workspace to isolate investigations
uv run python main.py "Your question here" --workspace my-project --budget 10

# List all workspaces
uv run python main.py --list-workspaces

# List questions in a specific workspace
uv run python main.py --list --workspace my-project

# Name a run for easier identification in the trace viewer
uv run python main.py "Your question here" --name "baseline v2" --budget 10

# Retroactively stage a completed run (hides its effects from baseline readers)
uv run python main.py --stage-run RUN_ID

# Commit a staged run (makes its effects visible to all readers)
uv run python main.py --commit-run RUN_ID

# Smoke-test mode: uses Haiku, fewer agent rounds, budget defaults to 1
uv run python main.py "Your question here" --smoke-test

# Any command can target the production database
uv run python main.py --db prod --list

# Run an orchestrator on Kubernetes (against the prod DB) instead of locally.
# `--prod` is a shorthand for `--db prod --executor prod`; only orchestrator
# runs (a question + budget) can be submitted as a Job. See "Run on Kubernetes"
# below for prerequisites.
uv run python main.py "Your question here" --budget 20 --prod

# Select an available-moves preset (controls which tools are available per call type)
uv run python main.py "Your question" --available-moves default --budget 10

# Select an available-calls preset (controls which scouts/dispatches the two-phase orchestrator uses)
# 'default' = standard scouts, 'multi-subquestion' = replaces generic subquestions scout with web-questions and deep-questions scouts
uv run python main.py "Your question" --available-calls multi-subquestion --budget 20

# Select a view variant (the ever-evolving best summary of a question)
# 'sectioned' = importance-scored items (default), 'judgement' = flat NL judgement page
uv run python main.py "Your question" --view-variant judgement --budget 10

# Tune how many considerations each ingest call extracts (default: 4)
uv run python main.py --ingest FILE --for-question QUESTION_ID --ingest-num-claims 6 --budget 5

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

To compare two variants of the research pipeline (different configs, prompts, or code changes), use `scripts/ab_branch.sh`. This creates git worktrees for each arm, runs staged investigations concurrently, then launches evaluation agents that compare the results.

```bash
scripts/ab_branch.sh \
  --branch-a feature-x \
  --branch-b feature-y \
  --env-a .a.env \
  --env-b .b.env \
  --workspace ab-scratch \
  -- "'Is the sky blue?' --budget 10 --smoke-test"
```

| Flag | Required | Description |
|------|----------|-------------|
| `--branch-a` | Yes | Git branch for arm A |
| `--branch-b` | Yes | Git branch for arm B |
| `--env-a` | No (default: `.env`) | Env file for arm A |
| `--env-b` | No (default: `.env`) | Env file for arm B |
| `--eval-branch` | No (default: current branch) | Branch to run evaluation agents from |
| `--workspace` | No | Workspace name passed to main.py |

The script runs concurrent evaluation agents that compare the runs on: grounding & factual correctness, coverage & relevance, depth vs breadth, research redundancy, consistency, research progress, and general quality. Each agent independently evaluates both arms, then a comparison LLM produces a structured preference rating (7-point scale from "A strongly preferred" to "B strongly preferred"). A final LLM synthesizes all comparisons into an overall assessment.

Reports are saved to `data/ab-reports/` and to the `ab_eval_reports` database table. View them in the frontend at `/ab-evals`.

### Single-run evaluation

Evaluate a single staged run across all quality dimensions (grounding, coverage & relevance, depth vs breadth, research redundancy, consistency, research progress, general quality):

```bash
uv run python main.py --run-eval RUN_ID

# Run only specific evaluation agents (works with --run-eval and --ab-eval)
uv run python main.py --run-eval RUN_ID --eval-agents grounding,consistency
```

Reports are saved to `data/run-eval-reports/` and to the `run_eval_reports` database table.

### A/B evaluation (standalone)

You can also run the evaluation agents independently against any two staged runs:

```bash
uv run python main.py --ab-eval RUN_ID_A RUN_ID_B
```

### A/B evaluation UI

The frontend at `/ab-evals` provides:

- **Index page**: Lists all evaluations with question headline, colored preference indicators, and assessment preview
- **Detail page**: Overall assessment, preference summary grid, expandable per-dimension reports (with tabs for Comparison / Run A Report / Run B Report), links to all traces (research runs and evaluation agent runs), and side-by-side config diff highlighting differences between arms

### Run config tracking

Every run automatically captures its configuration (model, budget, call variants, available moves, git commit, etc.) to the `runs` table. This config is displayed:

- On every trace page, as a key-value table above the trace viewer
- On A/B eval detail pages, as a side-by-side comparison with amber highlighting for values that differ between arms

### Utility flags

| Flag | Description |
|------|-------------|
| `--run-id-file PATH` | Write the run_id to a file after DB creation (for scripted capture) |
| `--env-file PATH` | Load settings from this env file in addition to `.env` |

### Run on Kubernetes

Long or expensive prod runs can be submitted as Kubernetes Jobs in the GKE
cluster instead of running on your laptop. The CLI POSTs to the rumil API,
which creates the Job using its in-cluster ServiceAccount; logs stream back
to your terminal until the run finishes.

| Flag | Meaning |
|------|---------|
| `--db {prod,local}` | Which Supabase to target. Default: local. |
| `--executor {prod,local}` | Where the orchestrator runs. Default: local. |
| `--prod` | Shorthand for `--db prod --executor prod`. |

Constraints:

- `--executor prod` is only supported for orchestrator runs (a question with
  `--budget`). For `--list`, `--summary`, `--continue`, etc., use
  `--db prod` (or `--db prod --executor local`).
- `--db local --executor prod` is rejected — the cluster cannot reach a
  local Supabase.

One-time setup on your laptop, in `.env` or `.env.local`:

```bash
# Where the rumil API lives. Defaults to https://api.rumil.ink and only
# needs to be set if you're targeting a different deployment.
RUMIL_API_URL=https://api.rumil.ink

# Supabase HS256 secret used to mint short-lived CLI tokens. Same value as
# the cluster's rumil-api-secrets — get it from the prod env you already
# have, or ask in #infra.
SUPABASE_JWT_SECRET=...

# Optional: override the shared CLI service-account user (default lives in
# Settings). Set to your own Supabase user_id to attribute jobs to yourself.
# DEFAULT_CLI_USER_ID=...
```

Then:

```bash
# Submit a smoke-test orchestrator run against prod — cheapest end-to-end check
uv run python main.py "is the sky blue" --budget 1 --smoke-test \
  --workspace k8s-smoke --prod

# Equivalent explicit form
uv run python main.py "..." --budget 20 --workspace my-project \
  --db prod --executor prod
```

#### Experiments (run uncommitted code in the cluster)

`scripts/remote_run.sh` builds the current source tree into a one-off API
image, pushes it to the rumil Artifact Registry under a unique tag (e.g.
`exp-20260425-cafebabe-dirty`), then submits an orchestrator run pinned
to that tag — without touching the deployed `rumil-api`. Use this to
test changes against the prod database before they're merged or
deployed.

```bash
# One-time docker auth
gcloud auth configure-docker us-central1-docker.pkg.dev

# Run an experiment
./scripts/remote_run.sh "your question" --budget 5 --workspace exp-foo
```

The submitted Job's pod uses the experiment image while inheriting prod
secrets and env from the live `rumil-api` Deployment, so it sees the
same prod Supabase that any other `--prod` run would. Override the
target Artifact Registry repository with `RUMIL_IMAGE_REPOSITORY` if
you're running against a different project.

You can also pass `--container-tag TAG` to `main.py` directly if you've
already built and pushed the image yourself.

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

# Stop after a specific stage (build_context or update_workspace)
uv run python scripts/run_call.py find-considerations "Test question" --up-to-stage build_context
uv run python scripts/run_call.py find-considerations "Test question" --up-to-stage update_workspace

# Name a run for easier identification
uv run python scripts/run_call.py find-considerations "Test question" --name "context experiment"
```

The `--up-to-stage` flag truncates the call lifecycle. Each call runs three stages in order: `build_context` → `update_workspace` → `closing_review`. Passing `--up-to-stage build_context` runs only context assembly; `--up-to-stage update_workspace` skips the closing review. Useful for inspecting context or page output in isolation.

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
