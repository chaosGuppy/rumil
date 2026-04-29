#!/usr/bin/env bash
# Source frontend/.env.overrides (if present) into the environment, then exec
# the rest of the command. Lets overrides (per-worktree port numbers, dev
# preferences, etc.) layer on top of frontend/.env without relying on Node's
# --env-file flag, which can't be forwarded into Next.js's forked dev workers.
set -euo pipefail

if [[ -f .env.overrides ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env.overrides
  set +a
fi

exec "$@"
