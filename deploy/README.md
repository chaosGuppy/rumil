# Deploying rumil

Production runs on GKE Autopilot in GCP project `project-fe559f0f-d011-4af4-bf0`,
cluster `differential` in `us-central1`. The Helm chart lives in `deploy/chart/`;
build + push + roll happens via `./scripts/deploy.sh`.

## One-time onboarding

You need this once per developer machine. Once it's done, `./scripts/deploy.sh`
and `sops decrypt deploy/chart/secrets.enc.yaml` both Just Work.

### 1. Get the tools

```bash
brew bundle   # installs sops, helm, google-cloud-sdk (and the rest of the toolchain)
helm plugin install https://github.com/jkroepke/helm-secrets
gcloud components install gke-gcloud-auth-plugin
```

### 2. Authenticate gcloud — both kinds

```bash
gcloud auth login                          # for the gcloud CLI itself
gcloud auth application-default login      # for libraries (SOPS reads ADC, not gcloud)
gcloud config set project project-fe559f0f-d011-4af4-bf0
```

The second command is the one people forget. SOPS uses Application Default
Credentials, so without it you'll get a cryptic auth error even though
`gcloud auth list` shows you're signed in.

### 3. Get IAM access (ask an admin)

You need two grants on the GCP project, both to your Google account. Ask an
existing admin (currently Lawrence) to run:

```bash
# Project membership so you can see the project in your console
gcloud projects add-iam-policy-binding project-fe559f0f-d011-4af4-bf0 \
  --member="user:YOU@example.com" \
  --role="roles/browser"

# SOPS key access — encrypt + decrypt
gcloud kms keys add-iam-policy-binding sops-key \
  --keyring=sops --location=global \
  --project=project-fe559f0f-d011-4af4-bf0 \
  --member="user:YOU@example.com" \
  --role="roles/cloudkms.cryptoKeyEncrypterDecrypter"
```

If you only need to deploy (not edit secrets), `roles/cloudkms.cryptoKeyDecrypter`
is enough on the KMS key. `EncrypterDecrypter` covers both.

### 4. Get cluster credentials (only if you'll run `./scripts/deploy.sh`)

```bash
gcloud container clusters get-credentials differential \
  --region=us-central1 --project=project-fe559f0f-d011-4af4-bf0
```

This writes a kubeconfig context that points at the GKE cluster.

### 5. Smoke test

```bash
sops decrypt deploy/chart/secrets.enc.yaml > /dev/null && echo "SOPS OK"
kubectl get pods -n rumil                                   # if you set up cluster creds
```

## Editing secrets

```bash
# Add or rotate a single key (wraps `sops set`, reads value from stdin if omitted)
scripts/set-secret.sh VOYAGE_AI_API_KEY pa-xxxxxxxx
pbpaste | scripts/set-secret.sh VOYAGE_AI_API_KEY        # keep secret out of shell history
scripts/set-secret.sh --frontend INVITE_PASSWORD          # frontend section

# Or call sops directly:
sops set deploy/chart/secrets.enc.yaml '["secrets"]["api"]["MY_NEW_KEY"]' '"my-value"'

# Inspect the decrypted file
sops decrypt deploy/chart/secrets.enc.yaml | less

# Edit interactively (opens $EDITOR with the decrypted file, re-encrypts on save)
sops edit deploy/chart/secrets.enc.yaml
```

When adding a new key, also add it to `deploy/chart/secrets.yaml.template` so
future devs know it exists.

## Deploying

```bash
./scripts/deploy.sh --all                       # builds API + frontend, pushes, rolls
./scripts/deploy.sh --api                       # API only
./scripts/deploy.sh --frontend                  # frontend only
./scripts/deploy.sh --api --tag <some-tag>      # custom image tag
```

Defaults: image tag = current git short SHA, namespace = `rumil`, release = `rumil`.

## Common gotchas

- **`gcloud auth login` ≠ `gcloud auth application-default login`.** Both are
  required. The first authenticates the CLI; the second writes the credentials
  JSON that SOPS (and other GCP client libraries) actually read.
- **Multiple Google accounts.** ADC stores one set of credentials at a time.
  `gcloud auth application-default print-access-token` prints whoever is
  currently active there. Re-run the login command to switch.
- **New encrypted files** need a matching entry in `.sops.yaml`. Currently
  only `deploy/chart/secrets(\.enc)?\.yaml` is covered. Anything outside that
  glob won't be auto-encrypted by `sops` and the `creation_rules` machinery.
