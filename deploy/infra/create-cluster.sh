#!/usr/bin/env bash
# Provision the Differential Labs GKE Autopilot cluster and surrounding GCP
# resources in a fresh GCP project. Idempotent — safe to re-run.
#
# What this creates:
#   - Enables the GCP APIs we need
#   - A custom-mode VPC + regional subnet with pod/service secondary ranges
#   - A Cloud Router + Cloud NAT so private-node workloads can egress
#   - A dedicated node service account with minimum required roles
#   - A Docker-format Artifact Registry repo (rumil) in the cluster's region
#   - A regional GKE Autopilot cluster with Workload Identity + private nodes
#   - A Cloud DNS public managed zone for rumil.ink
#   - A kubectl context pointed at the new cluster
#
# Prereqs:
#   - gcloud authed as an identity with Owner on the target project
#   - Billing linked to the project
#
# Usage:
#   bash deploy/infra/create-cluster.sh

set -euo pipefail

PROJECT="project-fe559f0f-d011-4af4-bf0"
REGION="us-central1"
ACCOUNT="lawrence.gab.phillips@gmail.com"

CLUSTER_NAME="differential"
NETWORK_NAME="differential-vpc"
SUBNET_NAME="differential-subnet-${REGION}"
ROUTER_NAME="differential-router-${REGION}"
NAT_NAME="differential-nat-${REGION}"

NODE_SA_NAME="gke-differential-nodes"
NODE_SA="${NODE_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

AR_REPO="rumil"

DNS_ZONE_NAME="rumil-ink"
DNS_NAME="rumil.ink."

SUBNET_CIDR="10.0.0.0/20"
PODS_CIDR="10.4.0.0/14"
SERVICES_CIDR="10.8.0.0/20"

GC() { gcloud --project="$PROJECT" --account="$ACCOUNT" "$@"; }

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

exists() {
  # Run a `gcloud ... describe` as an existence check. Returns 0 if the
  # resource exists, nonzero otherwise.
  GC "$@" >/dev/null 2>&1
}

log "Target: project=$PROJECT region=$REGION cluster=$CLUSTER_NAME"

log "Enabling required GCP APIs"
GC services enable \
  container.googleapis.com \
  compute.googleapis.com \
  artifactregistry.googleapis.com \
  dns.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com

log "VPC: $NETWORK_NAME"
if exists compute networks describe "$NETWORK_NAME"; then
  echo "  already exists — skipping"
else
  GC compute networks create "$NETWORK_NAME" \
    --subnet-mode=custom \
    --bgp-routing-mode=regional
fi

log "Subnet: $SUBNET_NAME ($SUBNET_CIDR, pods=$PODS_CIDR, services=$SERVICES_CIDR)"
if exists compute networks subnets describe "$SUBNET_NAME" --region="$REGION"; then
  echo "  already exists — skipping"
else
  GC compute networks subnets create "$SUBNET_NAME" \
    --network="$NETWORK_NAME" \
    --region="$REGION" \
    --range="$SUBNET_CIDR" \
    --secondary-range="pods=$PODS_CIDR" \
    --secondary-range="services=$SERVICES_CIDR" \
    --enable-private-ip-google-access
fi

log "Cloud Router: $ROUTER_NAME"
if exists compute routers describe "$ROUTER_NAME" --region="$REGION"; then
  echo "  already exists — skipping"
else
  GC compute routers create "$ROUTER_NAME" \
    --network="$NETWORK_NAME" \
    --region="$REGION"
fi

log "Cloud NAT: $NAT_NAME"
if exists compute routers nats describe "$NAT_NAME" \
    --router="$ROUTER_NAME" --region="$REGION"; then
  echo "  already exists — skipping"
else
  GC compute routers nats create "$NAT_NAME" \
    --router="$ROUTER_NAME" \
    --region="$REGION" \
    --nat-all-subnet-ip-ranges \
    --auto-allocate-nat-external-ips
fi

log "Node service account: $NODE_SA"
if exists iam service-accounts describe "$NODE_SA"; then
  echo "  already exists — skipping create"
else
  GC iam service-accounts create "$NODE_SA_NAME" \
    --display-name="GKE differential node SA" \
    --description="Runtime identity for GKE Autopilot nodes in the differential cluster"
fi

log "Granting roles to node SA (idempotent)"
for role in \
  roles/logging.logWriter \
  roles/monitoring.metricWriter \
  roles/monitoring.viewer \
  roles/stackdriver.resourceMetadata.writer \
  roles/autoscaling.metricsWriter \
  roles/artifactregistry.reader; do
  echo "  $role"
  GC projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$NODE_SA" \
    --role="$role" \
    --condition=None \
    --quiet >/dev/null
done

log "Artifact Registry: $AR_REPO ($REGION, docker)"
if exists artifacts repositories describe "$AR_REPO" --location="$REGION"; then
  echo "  already exists — skipping"
else
  GC artifacts repositories create "$AR_REPO" \
    --repository-format=docker \
    --location="$REGION" \
    --description="Rumil container images"
fi

log "GKE Autopilot cluster: $CLUSTER_NAME (this takes ~5-10 minutes)"
if exists container clusters describe "$CLUSTER_NAME" --region="$REGION"; then
  echo "  already exists — skipping"
else
  GC container clusters create-auto "$CLUSTER_NAME" \
    --region="$REGION" \
    --release-channel=regular \
    --network="$NETWORK_NAME" \
    --subnetwork="$SUBNET_NAME" \
    --cluster-secondary-range-name=pods \
    --services-secondary-range-name=services \
    --service-account="$NODE_SA" \
    --enable-private-nodes
fi

log "Fetching kubectl credentials"
GC container clusters get-credentials "$CLUSTER_NAME" --region="$REGION"

log "Cloud DNS zone: $DNS_ZONE_NAME ($DNS_NAME)"
if exists dns managed-zones describe "$DNS_ZONE_NAME"; then
  echo "  already exists — skipping"
else
  GC dns managed-zones create "$DNS_ZONE_NAME" \
    --dns-name="$DNS_NAME" \
    --description="Public DNS zone for rumil.ink" \
    --visibility=public
fi

log "Done."
echo
echo "Cluster endpoint + current state:"
GC container clusters describe "$CLUSTER_NAME" --region="$REGION" \
  --format='value(endpoint,status,currentMasterVersion)'
echo
echo "Cloud DNS nameservers for rumil.ink (set these at your registrar):"
GC dns managed-zones describe "$DNS_ZONE_NAME" \
  --format='value(nameServers.list())' | tr ';' '\n' | sed 's/^/  /'
echo
echo "Next steps:"
echo "  1. Point the rumil.ink registrar at the nameservers above."
echo "  2. Verify: kubectl get nodes"
echo "  3. Install cert-manager + Gateway + HTTPRoutes and deploy the helm chart."
