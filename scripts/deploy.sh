#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHART_DIR="$REPO_ROOT/deploy/chart"
SECRETS_FILE="$CHART_DIR/secrets.enc.yaml"
NAMESPACE="rumil"
RELEASE="rumil"
API_REPO="us-central1-docker.pkg.dev/project-fe559f0f-d011-4af4-bf0/rumil/rumil-api"
FRONTEND_REPO="us-central1-docker.pkg.dev/project-fe559f0f-d011-4af4-bf0/rumil/rumil-frontend"

usage() {
    echo "Usage: $0 [--api] [--frontend] [--all] [--tag TAG]"
    echo ""
    echo "Builds, pushes, and deploys to Kubernetes."
    echo ""
    echo "  --api        Build and deploy the API"
    echo "  --frontend   Build and deploy the frontend"
    echo "  --all        Build and deploy both (default if none specified)"
    echo "  --tag TAG    Image tag (default: git short SHA)"
    exit 1
}

deploy_api=false
deploy_frontend=false
tag=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --api) deploy_api=true; shift ;;
        --frontend) deploy_frontend=true; shift ;;
        --all) deploy_api=true; deploy_frontend=true; shift ;;
        --tag) tag="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

if ! $deploy_api && ! $deploy_frontend; then
    deploy_api=true
    deploy_frontend=true
fi

if [[ -z "$tag" ]]; then
    tag="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
fi

echo "Tag: $tag"
echo "API: $deploy_api | Frontend: $deploy_frontend"
echo ""

if $deploy_api; then
    echo "==> Building API image (linux/amd64)..."
    docker build --platform linux/amd64 \
        -t "$API_REPO:$tag" \
        -f "$REPO_ROOT/deploy/Dockerfile.api" \
        "$REPO_ROOT"

    echo "==> Pushing API image..."
    docker push "$API_REPO:$tag"
fi

if $deploy_frontend; then
    # NEXT_PUBLIC_* vars must be set at build time — `next build` inlines them
    # into the client bundle. NEXT_PUBLIC_API_URL is intentionally unset so
    # client fetches go same-origin and the Next.js middleware can inject the
    # Supabase JWT before proxying to the backend service.
    FRONTEND_SUPABASE_URL="https://aesjaehibxrzearctiqp.supabase.co"
    if [[ -z "${NEXT_PUBLIC_SUPABASE_ANON_KEY:-}" ]]; then
        echo "error: NEXT_PUBLIC_SUPABASE_ANON_KEY is required for the frontend build." >&2
        echo "       copy it from the Supabase dashboard → Settings → API → anon public." >&2
        exit 1
    fi

    echo "==> Building frontend (pnpm build)..."
    cd "$REPO_ROOT/frontend"
    NEXT_PUBLIC_SUPABASE_URL="$FRONTEND_SUPABASE_URL" \
    NEXT_PUBLIC_SUPABASE_ANON_KEY="$NEXT_PUBLIC_SUPABASE_ANON_KEY" \
        pnpm build

    echo "==> Building frontend image (linux/amd64)..."
    docker build --platform linux/amd64 \
        -t "$FRONTEND_REPO:$tag" \
        -f "$REPO_ROOT/deploy/Dockerfile.frontend" \
        "$REPO_ROOT/frontend"

    echo "==> Pushing frontend image..."
    docker push "$FRONTEND_REPO:$tag"
    cd "$REPO_ROOT"
fi

echo "==> Deploying with Helm (release=$RELEASE, namespace=$NAMESPACE, tag=$tag)..."
helm secrets upgrade --install "$RELEASE" "$CHART_DIR" \
    -n "$NAMESPACE" \
    --create-namespace \
    --set "releaseId=$tag" \
    -f "$SECRETS_FILE"

echo "==> Waiting for rollout..."
if $deploy_api; then
    echo "  API:"
    kubectl rollout status "deployment/${RELEASE}-api" -n "$NAMESPACE" --timeout=120s
fi
if $deploy_frontend; then
    echo "  Frontend:"
    kubectl rollout status "deployment/${RELEASE}-frontend" -n "$NAMESPACE" --timeout=120s
fi

echo ""
echo "Deploy complete."
