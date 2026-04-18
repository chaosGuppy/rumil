default:
    @just --list

test *ARGS:
    uv run pytest {{ARGS}}

test-serial *ARGS:
    uv run pytest -n0 {{ARGS}}

test-llm *ARGS:
    uv run pytest --llm {{ARGS}}

lint:
    uv run ruff check .

format:
    uv run ruff check --fix . && uv run ruff format .

typecheck:
    uv run pyright

typecheck-frontend:
    cd frontend && npx tsc --noEmit

lint-frontend:
    cd frontend && pnpm lint

dev-api:
    ./scripts/dev-api.sh

dev-frontend:
    cd frontend && pnpm dev

precommit-all:
    uv run pre-commit run --all-files

generate-api-types:
    ./scripts/generate-api-types.sh
