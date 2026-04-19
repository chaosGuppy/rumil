---
name: rumil-search
description: Embedding-based search over the active rumil workspace. Takes a free-text query and returns the most semantically similar pages (questions, claims, judgements, concepts). Default mode is a fast top-N lookup with similarity scores — use proactively mid-conversation to check what the workspace already knows about a topic, find related research, or check for duplicates before creating a new question. Pass --full for the multi-tier context builder when you want a deeper rendered read.
allowed-tools: Bash
argument-hint: "<query> [--full] [--limit N]"
---

# rumil-search

> **Under the hood:** this skill calls `rumil_skills.search_workspace`.
> Default mode uses `rumil.embeddings.search_pages` (which calls the
> `match_pages` RPC defined in the migrations). `--full` delegates to
> `rumil.context.build_embedding_based_context` — the same multi-tier
> context builder used inside real rumil calls like `find_considerations`.
> Read-only; no API endpoint.

Free-text semantic search across the active workspace.

## Modes

- **default (quick)** — top-N pages at ABSTRACT detail with similarity
  scores. Fast and cheap. Use this first. `--limit N` controls the number
  of results (default 8).
- **`--full`** — the multi-tier context builder used inside real rumil
  calls. Returns a richer rendered block (full pages + summaries within
  configured char budgets). Reach for this when you want a deep read, not
  just a pointer list.

```!
setopt no_glob 2>/dev/null; set -f; PYTHONPATH=.claude/lib uv run python -m rumil_skills.search_workspace $ARGUMENTS
```
