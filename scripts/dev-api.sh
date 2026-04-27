#!/usr/bin/env bash
# Start the API dev server on the port the frontend expects.
# Reads frontend/.env.overrides (overrides, e.g. per-worktree) then
# frontend/.env (shared); the overrides file wins. Falls back to port 8000
# if neither sets the URL.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

PORT=8000
SOURCE=""
for env_file in "$PROJECT_ROOT/frontend/.env.overrides" "$PROJECT_ROOT/frontend/.env"; do
    [[ -f "$env_file" ]] || continue
    for var in API_BASE_URL NEXT_PUBLIC_API_URL; do
        url=$(grep -E "^${var}=" "$env_file" | cut -d= -f2- | tr -d '[:space:]')
        if [[ -n "$url" ]]; then
            extracted=$(echo "$url" | grep -oE ':[0-9]+$' | tr -d ':')
            if [[ -n "$extracted" ]]; then
                PORT="$extracted"
                SOURCE="$env_file"
                break 2
            fi
        fi
    done
done

echo "Starting API server on port $PORT${SOURCE:+ (from $SOURCE)}"
exec uv run uvicorn rumil.api.app:app --reload --port "$PORT"
