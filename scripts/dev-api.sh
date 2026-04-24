#!/usr/bin/env bash
# Start the API dev server on the port configured in frontend/.env.local.
# Falls back to port 8000 if .env.local doesn't exist or doesn't set a port.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$PROJECT_ROOT/frontend/.env.local"

PORT=8000
if [[ -f "$ENV_FILE" ]]; then
    for var in API_BASE_URL NEXT_PUBLIC_API_URL; do
        url=$(grep -E "^${var}=" "$ENV_FILE" | cut -d= -f2- | tr -d '[:space:]')
        if [[ -n "$url" ]]; then
            extracted=$(echo "$url" | grep -oE ':[0-9]+$' | tr -d ':')
            if [[ -n "$extracted" ]]; then
                PORT="$extracted"
                break
            fi
        fi
    done
fi

echo "Starting API server on port $PORT (from $ENV_FILE)"
exec uv run uvicorn rumil.api.app:app --reload --port "$PORT"
