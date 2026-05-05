---
name: rumil-load-run
description: Load a rumil run into the conversation by run ID. Shows a tree of every call in the run (id, type, status, cost, event summary) grouped under the run. Use when you have a trace URL like /traces/<run_id> and want to see the whole run, not a single call. For one call's verbatim trace use rumil-trace instead. Pass --compare <other_run_id> to A/B two runs side-by-side.
allowed-tools: Bash
argument-hint: "<run_id> [--full] [--only llm_exchange] [--last-n N] [--compare <other_run_id>]"
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
- `--compare <other_run_id>` — render the two runs side-by-side, aligned by
  call_type. Within each call_type, calls are paired by creation order
  (1st of-type-in-A vs 1st of-type-in-B, etc); extras land as `—` cells.
  Rows where the two sides differ are flagged with `*` and a brief reason
  (status, cost delta >25%, exchange count). No verbatim exchanges in compare
  mode — call summaries only (id, status, cost, event counts, fruit/conf).
  Use when A/B'ing two runs of similar shape (e.g. same workflow on different
  prefix variants, or two judging runs on different pairs).

Note: some runs (standalone dispatches) have no row in the `runs` table —
they exist only as a `calls.run_id` tag. This skill queries `calls` directly
so it works for both cases. If a `runs` row exists, its name + config are
printed as a header.

```!
setopt no_glob 2>/dev/null; set -f; PYTHONPATH=.claude/lib uv run python -m rumil_skills.load_run $ARGUMENTS
```
