#!/usr/bin/env bash
# Set up Vertex AI access for the rumil pods running in GKE.
#
# What this creates:
#   - A GSA `rumil-vertex-ai` with roles/aiplatform.user on the project
#   - Workload Identity bindings so the rumil-api and rumil-orchestrator-job
#     KSAs (in the `rumil` namespace) can impersonate the GSA
#
# The KSAs themselves are created by the helm chart (deploy/chart/templates/
# api-rbac.yaml); they need the iam.gke.io/gcp-service-account annotation
# pointing at this GSA, which is also baked into the chart.
#
# Idempotent — safe to re-run.
#
# Usage:
#   bash deploy/infra/setup-vertex-ai.sh

set -euo pipefail

PROJECT="project-fe559f0f-d011-4af4-bf0"
ACCOUNT="lawrence.gab.phillips@gmail.com"

GSA_NAME="rumil-vertex-ai"
GSA="${GSA_NAME}@${PROJECT}.iam.gserviceaccount.com"

K8S_NAMESPACE="rumil"
KSAS=(rumil-api rumil-orchestrator-job)

GC() { gcloud --project="$PROJECT" --account="$ACCOUNT" "$@"; }
log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
exists() { GC "$@" >/dev/null 2>&1; }

log "Ensuring Vertex AI API is enabled"
GC services enable aiplatform.googleapis.com --quiet >/dev/null

log "GSA: $GSA"
if exists iam service-accounts describe "$GSA"; then
  echo "  already exists — skipping create"
else
  GC iam service-accounts create "$GSA_NAME" \
    --display-name="Rumil Vertex AI access" \
    --description="Used by rumil-api and rumil-orchestrator-job pods to call Vertex AI"
fi

log "Granting roles/aiplatform.user on $PROJECT to $GSA"
GC projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$GSA" \
  --role="roles/aiplatform.user" \
  --condition=None \
  --quiet >/dev/null

for KSA in "${KSAS[@]}"; do
  log "Workload Identity binding: $K8S_NAMESPACE/$KSA -> $GSA"
  GC iam service-accounts add-iam-policy-binding "$GSA" \
    --role="roles/iam.workloadIdentityUser" \
    --member="serviceAccount:${PROJECT}.svc.id.goog[${K8S_NAMESPACE}/${KSA}]" \
    --quiet >/dev/null
done

log "Done."
echo
echo "Next: deploy the chart so the KSAs pick up the iam.gke.io/gcp-service-account"
echo "annotation, then bounce the pods (rolling restart on the api Deployment)."
