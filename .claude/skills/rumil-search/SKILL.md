---
name: rumil-search
description: Embedding-based search over the active rumil workspace. Takes a free-text query and returns the most semantically similar pages (questions, claims, judgements, concepts). Default mode is a fast top-N lookup with similarity scores — use proactively mid-conversation to check what the workspace already knows about a topic, find related research, or check for duplicates before creating a new question. Pass --full for the multi-tier context builder when you want a deeper rendered read.
allowed-tools: Bash
argument-hint: "<query> [--full] [--limit N]"
---

# rumil-search

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
PYTHONPATH=.claude/lib uv run python -m rumil_skills.search_workspace $ARGUMENTS
```
