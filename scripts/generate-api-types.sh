#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "Generating OpenAPI schema from FastAPI..."
cd "$REPO_ROOT"
uv run python -c "
from rumil.api.app import app
import json
schema = app.openapi()
with open('frontend/openapi.json', 'w') as f:
    json.dump(schema, f, indent=2, default=str)
"

echo "Generating TypeScript types for frontend/..."
cd "$REPO_ROOT/frontend"
pnpm exec openapi-ts

echo "Generating TypeScript types for parma/..."
cd "$REPO_ROOT/parma"
pnpm exec openapi-ts

echo "Done."
