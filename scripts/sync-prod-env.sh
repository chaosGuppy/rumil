#!/usr/bin/env bash
# Decrypt prod secrets via SOPS and write the prod-only ones into .env.
#
# Run this once after onboarding to enable `--executor prod` / `--prod`
# orchestrator runs. Re-run any time the upstream secrets rotate.
#
# Plaintext never touches disk: the decrypted output is piped from `sops`
# straight into a python helper that merges the relevant keys into .env
# in memory and writes the result atomically.
#
# Prerequisites: `brew install sops` and `gcloud auth application-default login`
# with access to the rumil KMS key (see deploy/README.md).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SECRETS_FILE="$REPO_ROOT/deploy/chart/secrets.enc.yaml"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"

# Vars whose values must match prod (not per-developer credentials).
PROD_SECRET_KEYS=(SUPABASE_JWT_SECRET SUPABASE_PROD_KEY VOYAGE_AI_API_KEY)

# Non-secret prod values written verbatim (KEY=VALUE pairs).
PROD_PLAIN_VALUES=(
    "SUPABASE_PROD_URL=https://aesjaehibxrzearctiqp.supabase.co"
    "RUMIL_API_URL=https://api.rumil.ink"
)

if ! command -v sops >/dev/null 2>&1; then
    echo "error: sops not installed. Install with: brew install sops" >&2
    exit 1
fi
if [[ ! -f "$SECRETS_FILE" ]]; then
    echo "error: $SECRETS_FILE not found" >&2
    exit 1
fi

# Resolve symlinks so we update the real .env file (in some worktrees .env is
# symlinked to a shared location).
ENV_TARGET="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$ENV_FILE")"
mkdir -p "$(dirname "$ENV_TARGET")"
[[ -e "$ENV_TARGET" ]] || : > "$ENV_TARGET"
chmod 600 "$ENV_TARGET" 2>/dev/null || true

read -r -d '' MERGE_PY <<'PY' || true
import json
import os
import sys
import tempfile

env_path = sys.argv[1]
n_secret_keys = int(sys.argv[2])
secret_keys = sys.argv[3 : 3 + n_secret_keys]
plain_pairs = sys.argv[3 + n_secret_keys :]

decrypted = json.load(sys.stdin) or {}
api = (decrypted.get("secrets") or {}).get("api") or {}
missing = [k for k in secret_keys if not api.get(k)]
if missing:
    print(f"error: missing keys in decrypted secrets: {missing}", file=sys.stderr)
    sys.exit(1)

values: dict[str, str] = {k: str(api[k]) for k in secret_keys}
for pair in plain_pairs:
    name, _, val = pair.partition("=")
    values[name] = val

with open(env_path, encoding="utf-8") as fh:
    lines = fh.readlines()

written: set[str] = set()
out: list[str] = []
for line in lines:
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        out.append(line)
        continue
    name = stripped.split("=", 1)[0].strip()
    if name in values:
        out.append(f"{name}={values[name]}\n")
        written.add(name)
    else:
        out.append(line)

remaining = [k for k in values if k not in written]
if remaining:
    if out and not out[-1].endswith("\n"):
        out.append("\n")
    for k in remaining:
        out.append(f"{k}={values[k]}\n")

dir_ = os.path.dirname(os.path.abspath(env_path)) or "."
fd, tmp = tempfile.mkstemp(prefix=".env.", dir=dir_)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.writelines(out)
    os.chmod(tmp, 0o600)
    os.replace(tmp, env_path)
except Exception:
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    raise

print(f"wrote {len(values)} prod var(s) to {env_path}: {', '.join(values)}")
PY

sops decrypt --output-type json "$SECRETS_FILE" | python3 -c "$MERGE_PY" \
    "$ENV_TARGET" \
    "${#PROD_SECRET_KEYS[@]}" "${PROD_SECRET_KEYS[@]}" \
    "${PROD_PLAIN_VALUES[@]}"
