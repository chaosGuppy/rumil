---
name: rumil-search
description: Embedding-based search over the active rumil workspace. Takes a free-text query and returns the most semantically similar pages (questions, claims, judgements, concepts). Use when the user asks what rumil knows about a topic, wants to find related research, or is checking for duplicates before creating a new question.
allowed-tools: Bash
argument-hint: "<query>"
---

# rumil-search

Free-text semantic search across the active workspace. Returns the
most-similar pages ranked by embedding distance, rendered compactly so
Claude can scan the output and pick what to dig into.

```!
PYTHONPATH=.claude/lib uv run python -m rumil_skills.search_workspace $ARGUMENTS
```
