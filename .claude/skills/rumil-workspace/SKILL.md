---
name: rumil-workspace
description: Show, list, or set the active rumil workspace for this Claude Code session. Workspace is rumil's scoping primitive — every other rumil-* skill reads it. Use with no args to list; pass a name to switch.
allowed-tools: Bash
argument-hint: "[set <name> | list]"
---

# rumil-workspace

Manages the active rumil workspace for this Claude Code session, persisted to
`.claude/state/rumil-session.json`. Every other `rumil-*` skill reads from
this state, so switching here affects the whole session.

```!
setopt no_glob 2>/dev/null; set -f; PYTHONPATH=.claude/lib uv run python -m rumil_skills.workspace $ARGUMENTS
```
