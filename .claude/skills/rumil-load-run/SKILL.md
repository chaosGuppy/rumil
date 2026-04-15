---
name: rumil-load-run
description: Load a rumil run into the conversation by run ID. Shows a tree of every call in the run (id, type, status, cost, event summary) grouped under the run. Use when you have a trace URL like /traces/<run_id> and want to see the whole run, not a single call. For one call's verbatim trace use rumil-trace instead.
allowed-tools: Bash
argument-hint: "<run_id> [--full] [--only llm_exchange] [--last-n N]"
---

# rumil-load-run

Loads every call that shares a `run_id` — the anchor the frontend uses for
`/traces/<run_id>`. Resolves short (8-char) or full run IDs.

Default output is a compact tree: one line per call with short id, type,
status, cost, duration, and a one-line event summary. Use this first to
find the interesting call, then drill into it with `rumil-trace <short_id>`.

Flags:
- `--full` — also print each call's full trace (events + verbatim exchanges)
- `--only <event>` — filter events in the per-call summary
- `--last-n N` — in `--full` mode, trim each call to its last N exchanges

Note: some runs (standalone dispatches) have no row in the `runs` table —
they exist only as a `calls.run_id` tag. This skill queries `calls` directly
so it works for both cases. If a `runs` row exists, its name + config are
printed as a header.

```!
setopt no_glob 2>/dev/null; set -f; PYTHONPATH=.claude/lib uv run python -m rumil_skills.load_run $ARGUMENTS
```
