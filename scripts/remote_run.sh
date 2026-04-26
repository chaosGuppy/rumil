#!/usr/bin/env bash
# Build the current source tree into a one-off API image, push it to the
# rumil Artifact Registry, then submit an orchestrator run as a k8s Job
# pinned to that tag. All arguments are forwarded to main.py.
#
# Use this for experiments where you want to test uncommitted code in the
# real cluster against the prod database without touching the deployed
# rumil-api or rumil-frontend.
#
# Prereqs (one-time):
#   gcloud auth login
#   gcloud auth configure-docker us-central1-docker.pkg.dev
#   uv sync
#   SUPABASE_JWT_SECRET set in your env (see CONTRIBUTING.md)
#
# Example:
#   ./scripts/remote_run.sh "is the sky blue?" --budget 5 --workspace exp-foo

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
API_REPO="${RUMIL_IMAGE_REPOSITORY:-us-central1-docker.pkg.dev/project-fe559f0f-d011-4af4-bf0/rumil/rumil-api}"

if [[ $# -lt 1 ]]; then
    echo "usage: $0 \"question\" --budget N [--workspace W] [other main.py flags]" >&2
    exit 2
fi

ts="$(date -u +%Y%m%d-%H%M%S)"
sha="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)"
dirty=""
if ! git -C "$REPO_ROOT" diff-index --quiet HEAD 2>/dev/null; then
    dirty="-dirty"
fi
tag="exp-${ts}-${sha}${dirty}"
image="${API_REPO}:${tag}"

echo "==> Building $image (linux/amd64)..." >&2
docker build --platform linux/amd64 \
    -t "$image" \
    -f "$REPO_ROOT/deploy/Dockerfile.api" \
    "$REPO_ROOT"

echo "==> Pushing $image..." >&2
docker push "$image"

echo "==> Submitting orchestrator run with --container-tag $tag..." >&2
exec uv run --project "$REPO_ROOT" python "$REPO_ROOT/main.py" \
    "$@" --prod --container-tag "$tag"
