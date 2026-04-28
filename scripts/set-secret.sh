#!/usr/bin/env bash
# Add or rotate a single key in deploy/chart/secrets.enc.yaml without
# dumping the decrypted file to disk. Wraps `sops set`, which decrypts,
# patches, and re-encrypts in one step.
#
# Usage:
#   scripts/set-secret.sh [--frontend] KEY [VALUE]
#
# If VALUE is omitted, the value is read from stdin (recommended — keeps
# the secret out of shell history). Trailing newlines on stdin are stripped.
#
# Examples:
#   scripts/set-secret.sh VOYAGE_AI_API_KEY pa-xxxxxxxx
#   pbpaste | scripts/set-secret.sh VOYAGE_AI_API_KEY
#   scripts/set-secret.sh --frontend INVITE_PASSWORD
#
# Prerequisites: sops on PATH and `gcloud auth application-default login`
# with access to the rumil KMS key (see deploy/README.md).
set -euo pipefail

SECTION="api"
if [[ "${1:-}" == "--frontend" ]]; then
    SECTION="frontend"
    shift
elif [[ "${1:-}" == "--api" ]]; then
    shift
fi

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "usage: $(basename "$0") [--frontend] KEY [VALUE]" >&2
    exit 2
fi

KEY="$1"
if [[ $# -eq 2 ]]; then
    VALUE="$2"
else
    if [[ -t 0 ]]; then
        echo "error: no VALUE arg and stdin is a tty — pipe the value in or pass it as the second arg" >&2
        exit 2
    fi
    VALUE="$(cat)"
    VALUE="${VALUE%$'\n'}"
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SECRETS_FILE="$REPO_ROOT/deploy/chart/secrets.enc.yaml"
TEMPLATE_FILE="$REPO_ROOT/deploy/chart/secrets.yaml.template"

if ! command -v sops >/dev/null 2>&1; then
    echo "error: sops not installed. Install with: brew install sops" >&2
    exit 1
fi
if [[ ! -f "$SECRETS_FILE" ]]; then
    echo "error: $SECRETS_FILE not found" >&2
    exit 1
fi

JSON_VALUE="$(KEY="$KEY" VALUE="$VALUE" python3 -c 'import json, os, sys; sys.stdout.write(json.dumps(os.environ["VALUE"]))')"
JSON_KEY="$(KEY="$KEY" python3 -c 'import json, os, sys; sys.stdout.write(json.dumps(os.environ["KEY"]))')"

sops set "$SECRETS_FILE" "[\"secrets\"][\"$SECTION\"][$JSON_KEY]" "$JSON_VALUE"

echo "ok: set secrets.$SECTION.$KEY in $SECRETS_FILE"
if ! grep -q "^[[:space:]]*$KEY:" "$TEMPLATE_FILE" 2>/dev/null; then
    echo "note: '$KEY' is not in $TEMPLATE_FILE — add a placeholder entry there so future devs know it exists." >&2
fi
