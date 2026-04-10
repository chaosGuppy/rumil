---
name: rumil-find-confusion
description: Scan recent rumil calls for signs of model confusion. Default mode uses fast, free heuristics (error events, non-complete status, exchange errors, cost outliers, thin output). Pass --deep for an LLM-based structured verdict on the top heuristic candidates, cached in scan log. Use when the user wants to review recent runs for quality, triage a batch of calls, or find specific traces worth inspecting.
allowed-tools: Bash
argument-hint: "[--limit N] [--deep [--deep-limit K] [--model ...]] [--force-rescan]"
---

# rumil-find-confusion

Triages recent calls in the active workspace, surfacing ones that look
off. Two modes:

**Heuristic (default)** — fast, deterministic, **no LLM cost** (only
local Supabase reads + arithmetic). Scores each recent call by:
- hard signals: non-complete status, trace error events, exchange errors
- soft signals: cost outliers (> 3× median), thin output relative to
  input, multiple warnings

**Deep (`--deep`)** — for the top heuristic candidates, runs a meta LLM
call with a shared system prompt (designed for prompt-cache reuse
across many scans) and a structured `ConfusionVerdict` schema.
**Costs per-scan LLM tokens**, roughly:
- `claude-haiku-4-5-20251001` — ~$0.005 per trace
- `claude-sonnet-4-6` (default) — ~$0.02-0.05 per trace
- `claude-opus-4-6` — ~$0.10-0.20 per trace

Exact cost depends on trace size (bigger traces = more tokens in the
user message). Verdicts are cached in
`.claude/state/rumil-scan-log.json`, so re-running the skill on a
trace already in the log doesn't re-pay. Force a re-scan with
`--force-rescan`.

Override model with `--model <id>` (e.g. haiku for cheap bulk scans).

## Invocation

```!
PYTHONPATH=.claude/lib uv run python -m rumil_skills.find_confusion $ARGUMENTS
```

## After it runs

Heuristic output is a ranked list: short ID, call type, signals, score.
Deep output adds a structured verdict per call — primary symptom,
severity, evidence quotes, suggested action (`inspect`, `redispatch`,
`edit_prompt:<file>`, `ignore`).

For any call that looks worth investigating, the natural next step is
`/rumil-trace <call_id>` to read the full exchanges, or
`/rumil-review <question_id>` if the issue seems question-wide.
