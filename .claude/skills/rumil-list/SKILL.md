---
name: rumil-list
description: List root questions in the active rumil workspace. Shows short ID, date, and headline for each. Use this as a lightweight overview before diving into a specific question with rumil-show.
allowed-tools: Bash
argument-hint: "[--workspace <name>]"
---

# rumil-list

> **Under the hood:** this skill calls `rumil_skills.list_questions`,
> which reads directly via `DB.get_root_questions()` (the same method
> powering the frontend's root-question list and the `get_root_questions`
> RPC). Read-only — no dispatch function, no API endpoint.

Lists root (top-level) questions in the active rumil workspace. Use
`/rumil-workspace` first if you need to switch workspaces.

```!
setopt no_glob 2>/dev/null; set -f; PYTHONPATH=.claude/lib uv run python -m rumil_skills.list_questions $ARGUMENTS
```
