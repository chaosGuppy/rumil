# Contributing

Quick-reference for working on this repo. For the big-picture architecture, see [CLAUDE.md](./CLAUDE.md).

## Setup

```bash
# 1. System tools (macOS / Linux with Homebrew)
brew bundle                                     # installs uv, node, pnpm, supabase CLI, just
# Windows: install the equivalents via winget/scoop. A Docker runtime
# (Docker Desktop, OrbStack, or colima) is a separate prerequisite on any OS.

# 2. Install Python deps (uv manages the venv)
uv sync

# 3. Start local Supabase (Docker required)
supabase start

# 4. Env files
cp .env.template .env                           # fill in ANTHROPIC_API_KEY
cp frontend/.env.template frontend/.env

# 5. Install git pre-commit hook
uv run pre-commit install
```

If `pre-commit install` errors with `Cowardly refusing to install hooks with core.hooksPath set`, your repo has a local `core.hooksPath` override. Either unset it (`git config --unset-all core.hooksPath`) or run hooks manually via `just precommit-all` / `uv run pre-commit run --all-files`.

## Auth in local dev

The deployed app has two gates: an invite password (`/welcome`) and Google OAuth via Supabase (`/sign-in`). For local dev you almost always want to skip both — the example env files do this by default.

- Set `AUTH_ENABLED=0` in **both** `.env` (backend) and `frontend/.env` (frontend). The example files ship with this set. The middleware then skips the invite cookie and the Supabase session check, and the API skips JWT verification. The two sides must agree — if only the frontend has it, every `/api/*` call 401s.
- To exercise the real flow locally, unset `AUTH_ENABLED` (or set to `1`) and fill in `INVITE_PASSWORD`, `INVITE_SECRET`, and the `NEXT_PUBLIC_SUPABASE_*` vars in `frontend/.env`. Local Google OAuth isn't wired up in `supabase/config.toml`, so the `/sign-in` button won't actually complete a flow against `supabase start` — you'd need to point at a real Supabase project.

Local overrides (per-worktree port numbers, individual dev preferences) live in `.env.overrides` and `frontend/.env.overrides`, both `.gitignore`d and layered on top of `.env` / `frontend/.env` at runtime. The workmux `post_create` hook writes them automatically when you spin up a worktree, but they're plain files — you can also write them by hand without any worktree setup.

### Submitting orchestrator runs to Kubernetes (`--executor prod`)

To use `--prod` (or `--db prod --executor prod`) the laptop needs to mint a short-lived Supabase JWT that the API verifies via the same `Depends(get_current_user)` flow the frontend uses. Set in your `.env`:

- `SUPABASE_JWT_SECRET` — same HS256 secret as the prod `rumil-api-secrets`. The CLI signs JWTs with it.
- `RUMIL_API_URL` — only needs to be set to override the default `https://api.rumil.ink`.
- `DEFAULT_CLI_USER_ID` — optional. Defaults to a shared CLI service-account user baked into `Settings`. Set this to attribute jobs to a specific Supabase user instead.

You don't need `kubectl` or a kubeconfig — the API does the cluster work for you.

## Common tasks

A [justfile](./justfile) wraps the commands below — `brew bundle` installs `just` (or `brew install just` on its own). Run `just --list` to see recipes. Direct commands work too.

| Task | Command |
| --- | --- |
| Run tests (parallel, default) | `uv run pytest` |
| Run tests serially (for debugging) | `uv run pytest -n0` |
| Run LLM-gated tests | `uv run pytest --llm` |
| Lint | `uv run ruff check .` |
| Format | `uv run ruff check --fix . && uv run ruff format .` |
| Type-check backend | `uv run pyright` |
| Type-check frontend | `cd frontend && npx tsc --noEmit` |
| Lint frontend | `cd frontend && pnpm lint` |
| Start API server | `./scripts/dev-api.sh` |
| Start frontend | `cd frontend && pnpm dev` |
| Run all pre-commit hooks | `uv run pre-commit run --all-files` |

## Testing

- Tests live in `tests/`. `conftest.py` provides `tmp_db` (unique project-scoped DB), plus fixtures for questions, calls, etc.
- Tests are parallelised with `pytest-xdist` (`-n auto --dist worksteal`) by default.
- Two opt-in flags gate slow/external tests: `--llm` (real Anthropic API) and `--integration` (implies `--llm`).
- CI runs the default non-LLM suite.

## Hooks: Claude vs pre-commit

Two hook systems run in parallel — both are intentional:

- **`.claude/settings.json`** — PostToolUse hooks that fire when Claude Code edits files. Runs ruff + pyright on every Python edit, `tsc` on every TypeScript edit, and regenerates API types when `schemas.py` / `models.py` changes. Humans editing files directly do **not** trigger these.
- **`.pre-commit-config.yaml`** — runs on `git commit`. Covers ruff (check + format), end-of-file / whitespace fixes, large-file / private-key guards, and frontend ESLint. Does **not** run pyright (too slow for every commit — CI handles it).

Both running ruff is fine; ruff is idempotent on already-formatted code.

## Database

- Local Supabase by default. **Never** pass `--prod` unless explicitly asked.
- Migrations live in `supabase/migrations/`. Create new ones with `supabase migration new <name>`.
- Apply pending migrations locally with `supabase migration up` — never `supabase db reset` (destroys data).

## Frontend types

`frontend/src/api/` is auto-generated from the backend OpenAPI schema. Don't edit those files; they regenerate via a PostToolUse hook when `schemas.py` or `models.py` changes, or manually with `./scripts/generate-api-types.sh`.
