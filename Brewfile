# Install with: brew bundle
# Works on macOS and on Linux (Homebrew on Linux is supported).
# Everything else (ruff, pyright, pytest, pre-commit) comes via `uv sync`.
#
# Prerequisite not listed here: a Docker runtime for local Supabase.
# On macOS: Docker Desktop, OrbStack, or colima. On Linux: your distro's docker.io.

# Python env manager
brew "uv"

# Frontend toolchain
brew "node"
brew "pnpm"

# Local database CLI
tap "supabase/tap"
brew "supabase/tap/supabase"

# Task runner for the repo's justfile
brew "just"

# Deployment toolchain — needed to read/edit deploy/chart/secrets.enc.yaml
# and to run ./scripts/deploy.sh against the GKE cluster.
brew "sops"
brew "helm"
cask "google-cloud-sdk"
