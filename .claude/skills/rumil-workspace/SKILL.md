---
name: rumil-workspace
description: Show, list, or set the active rumil workspace for this Claude Code session. Workspace is rumil's scoping primitive — every other rumil-* skill reads it. Use with no args to list; pass a name to switch.
allowed-tools: Bash
argument-hint: "[set <name> | list]"
---

# rumil-workspace

> **Under the hood:** this skill calls `rumil_skills.workspace`, which
> uses `DB.list_projects` for the list view and manipulates the JSON
> file at `.claude/state/rumil-session.json` via
> `rumil_skills._runctx.load_session_state` / `save_session_state`.
> Projects correspond to rows in the `projects` table (same concept as
> the CLI `--workspace` flag and `GET /api/projects`). Read-only on the
> DB side; writes only to the local session file.

Manages the active rumil workspace for this Claude Code session, persisted to
`.claude/state/rumil-session.json`. Every other `rumil-*` skill reads from
this state, so switching here affects the whole session.

```!
setopt no_glob 2>/dev/null; set -f; PYTHONPATH=.claude/lib uv run python -m rumil_skills.workspace $ARGUMENTS
```
