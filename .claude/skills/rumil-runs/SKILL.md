---
name: rumil-runs
description: List recent runs in the active rumil workspace with status, cost, and timing. Filterable by name (substring or LIKE pattern) and status; --summary prints aggregate counts and total cost. Use whenever a batch script (versus iterate, A/B branch, multi-essay sweep, fresh dispatch fan-out) just fired N runs and you want to see which are done, which are still running, and what they cost — fills the gap between rumil-trace (one call) and rumil-load-run (one run).
allowed-tools: Bash
argument-hint: "[--name <substr>] [--like <pattern>] [--status <s>] [--limit N] [--summary] [--workspace <name>]"
---

# rumil-runs

Lists recent runs in the active rumil workspace. Defaults to the last 20
ordered by `started_at` descending. Filters compose: `--like` is a raw
Postgres LIKE pattern (use `%` wildcards), `--name` is a case-insensitive
substring match (ILIKE), `--status` matches exactly.

`--summary` collapses to aggregate counts (per status) plus total cost —
useful right after kicking off a batch when you want one number, not a table.

```!
setopt no_glob 2>/dev/null; set -f; PYTHONPATH=.claude/lib uv run python -m rumil_skills.list_runs $ARGUMENTS
```
