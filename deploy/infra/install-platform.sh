#!/usr/bin/env bash
# Install the platform pieces that the rumil helm chart expects:
#   - cert-manager (via helm)
#   - Workload Identity so cert-manager can edit the Cloud DNS zone
#   - A Let's Encrypt ClusterIssuer using the Cloud DNS DNS-01 solver
#   - A reserved global external IP for the shared Gateway
#   - Cloud DNS A records for rumil.ink / api.rumil.ink
#   - A Certificate covering both hostnames
#   - A shared Gateway (gke-l7-global-external-managed) in the cert-manager ns
#
# The existing chart's HTTPRoutes already target `shared-tls-gateway` in the
# `cert-manager` namespace, so once this script completes and DNS propagates,
# enabling httproute.enabled=true on the chart is all that's needed.
#
# Idempotent — safe to re-run.
#
# Usage:
#   bash deploy/infra/install-platform.sh

set -euo pipefail

PROJECT="project-fe559f0f-d011-4af4-bf0"
REGION="us-central1"
ACCOUNT="lawrence.gab.phillips@gmail.com"

DNS_ZONE_NAME="rumil-ink"
APEX_DOMAIN="rumil.ink"
API_HOST="api.rumil.ink"

CERT_MANAGER_VERSION="1.19.2"
CERT_MANAGER_NS="cert-manager"

DNS_GSA_NAME="cert-manager-dns01"
DNS_GSA="${DNS_GSA_NAME}@${PROJECT}.iam.gserviceaccount.com"

GATEWAY_NAME="shared-tls-gateway"

CERT_NAME="rumil-ink-certificate"
CERT_SECRET_NAME="rumil-ink-tls"
CLUSTER_ISSUER_NAME="letsencrypt-dns01"
LE_EMAIL="lawrence.gab.phillips@gmail.com"

GC() { gcloud --project="$PROJECT" --account="$ACCOUNT" "$@"; }
log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
exists() { GC "$@" >/dev/null 2>&1; }

TXN_STARTED=0
cleanup_txn() { [[ "$TXN_STARTED" == "1" ]] && GC dns record-sets transaction abort --zone="$DNS_ZONE_NAME" >/dev/null 2>&1 || true; }
trap cleanup_txn EXIT

upsert_a_record() {
  local name="$1"
  local ip="$2"
  local current
  current="$(GC dns record-sets list --zone="$DNS_ZONE_NAME" \
    --name="${name}." --type=A \
    --format='value(rrdatas[0])' 2>/dev/null || echo "")"
  if [[ "$current" == "$ip" ]]; then
    echo "  $name -> $ip already correct"
    return
  fi
  GC dns record-sets transaction start --zone="$DNS_ZONE_NAME" >/dev/null
  TXN_STARTED=1
  if [[ -n "$current" ]]; then
    echo "  $name: replacing $current -> $ip"
    GC dns record-sets transaction remove --zone="$DNS_ZONE_NAME" \
      --name="${name}." --type=A --ttl=300 "$current" >/dev/null
  else
    echo "  $name: creating -> $ip"
  fi
  GC dns record-sets transaction add --zone="$DNS_ZONE_NAME" \
    --name="${name}." --type=A --ttl=300 "$ip" >/dev/null
  GC dns record-sets transaction execute --zone="$DNS_ZONE_NAME" >/dev/null
  TXN_STARTED=0
}

log "GSA for cert-manager Cloud DNS access: $DNS_GSA"
if exists iam service-accounts describe "$DNS_GSA"; then
  echo "  already exists — skipping create"
else
  GC iam service-accounts create "$DNS_GSA_NAME" \
    --display-name="cert-manager DNS-01 solver" \
    --description="Used by cert-manager to solve ACME DNS-01 challenges in Cloud DNS"
fi

log "Granting roles/dns.admin to $DNS_GSA"
GC projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$DNS_GSA" \
  --role="roles/dns.admin" \
  --condition=None \
  --quiet >/dev/null

log "Binding KSA cert-manager/cert-manager to $DNS_GSA via Workload Identity"
GC iam service-accounts add-iam-policy-binding "$DNS_GSA" \
  --role="roles/iam.workloadIdentityUser" \
  --member="serviceAccount:${PROJECT}.svc.id.goog[${CERT_MANAGER_NS}/cert-manager]" \
  --quiet >/dev/null

log "Installing cert-manager ($CERT_MANAGER_VERSION) via helm"
helm repo add jetstack https://charts.jetstack.io --force-update >/dev/null
helm repo update jetstack >/dev/null
helm upgrade --install cert-manager jetstack/cert-manager \
  --namespace "$CERT_MANAGER_NS" \
  --create-namespace \
  --version "$CERT_MANAGER_VERSION" \
  --set crds.enabled=true \
  --set startupapicheck.enabled=false \
  --set "global.leaderElection.namespace=$CERT_MANAGER_NS" \
  --set "serviceAccount.annotations.iam\\.gke\\.io/gcp-service-account=$DNS_GSA" \
  --wait

log "Rolling cert-manager deployments to pick up Workload Identity annotation"
kubectl -n "$CERT_MANAGER_NS" rollout restart deployment cert-manager
kubectl -n "$CERT_MANAGER_NS" rollout status  deployment cert-manager --timeout=180s

log "Applying ClusterIssuer, Gateway, and Certificate manifests"
kubectl apply -f - <<EOF
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: ${CLUSTER_ISSUER_NAME}
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: ${LE_EMAIL}
    privateKeySecretRef:
      name: ${CLUSTER_ISSUER_NAME}-account-key
    solvers:
      - dns01:
          cloudDNS:
            project: ${PROJECT}
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: ${CERT_NAME}
  namespace: ${CERT_MANAGER_NS}
spec:
  secretName: ${CERT_SECRET_NAME}
  issuerRef:
    name: ${CLUSTER_ISSUER_NAME}
    kind: ClusterIssuer
  commonName: ${APEX_DOMAIN}
  dnsNames:
    - ${APEX_DOMAIN}
    - ${API_HOST}
---
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: ${GATEWAY_NAME}
  namespace: ${CERT_MANAGER_NS}
spec:
  gatewayClassName: gke-l7-global-external-managed
  listeners:
    - name: https
      protocol: HTTPS
      port: 443
      tls:
        mode: Terminate
        certificateRefs:
          - name: ${CERT_SECRET_NAME}
      allowedRoutes:
        namespaces:
          from: All
EOF

log "Waiting for Gateway to be programmed (GKE provisions the LB and assigns an IP)"
for i in {1..40}; do
  GATEWAY_IP="$(kubectl -n "$CERT_MANAGER_NS" get gateway "$GATEWAY_NAME" \
    -o jsonpath='{.status.addresses[0].value}' 2>/dev/null || echo "")"
  if [[ -n "$GATEWAY_IP" ]]; then
    echo "  Gateway IP: $GATEWAY_IP"
    break
  fi
  sleep 15
done
if [[ -z "${GATEWAY_IP:-}" ]]; then
  echo "  Gateway did not acquire an IP within 10 minutes. Check:"
  echo "    kubectl -n $CERT_MANAGER_NS describe gateway $GATEWAY_NAME"
  exit 1
fi

log "Cloud DNS A records: $APEX_DOMAIN and $API_HOST -> $GATEWAY_IP"
upsert_a_record "$APEX_DOMAIN" "$GATEWAY_IP"
upsert_a_record "$API_HOST" "$GATEWAY_IP"

log "Done."
echo
echo "Gateway: $GATEWAY_NAME (IP: $GATEWAY_IP, class: gke-l7-global-external-managed)"
echo "DNS:     $APEX_DOMAIN and $API_HOST point at $GATEWAY_IP"
echo
echo "The certificate will issue once the registrar is pointed at the Cloud DNS"
echo "nameservers and DNS-01 challenges can be resolved."
echo
echo "Check progress with:"
echo "  kubectl -n ${CERT_MANAGER_NS} get certificate,challenge"
echo "  kubectl -n ${CERT_MANAGER_NS} get gateway ${GATEWAY_NAME}"
