---
name: rumil-list
description: List root questions in the active rumil workspace. Shows short ID, date, and headline for each. Use this as a lightweight overview before diving into a specific question with rumil-show.
allowed-tools: Bash
argument-hint: "[--workspace <name>]"
---

# rumil-list

Lists root (top-level) questions in the active rumil workspace. Use
`/rumil-workspace` first if you need to switch workspaces.

```!
PYTHONPATH=.claude/lib uv run python -m rumil_skills.list_questions $ARGUMENTS
```
