---
name: rumil-trace
description: Dump a rumil call's full execution trace — trace events plus every LLM exchange verbatim (system prompt, user message, response, tool calls). Use to inspect what happened inside a specific call, debug model confusion, or review a run for quality. Takes a short (8-char) or full call ID.
allowed-tools: Bash
argument-hint: "<call_id> [--brief] [--only llm_exchange] [--last-n N]"
---

# rumil-trace

> **Under the hood:** this skill calls `rumil_skills.trace`, which reads
> `DB.get_call_trace` (events from the `calls.trace_json` column) plus a
> direct `call_llm_exchanges` table read for verbatim exchanges. Same
> data the frontend trace view surfaces via `GET /api/calls/{call_id}`
> and `GET /api/calls/{call_id}/events`. Read-only — no dispatch
> function involved.

Loads a call's full trace into the Claude Code conversation. By default
every LLM exchange is printed verbatim so you (and Claude) can see the
model's actual words — summaries lose the signal needed to spot
confusion.

Use filters for large traces:
- `--brief` shortens bodies and hides the system prompt
- `--only <event>` shows only events of one type (e.g. `llm_exchange`, `error`)
- `--last-n N` trims to the final N exchanges
- `--no-exchanges` shows just the event timeline

```!
setopt no_glob 2>/dev/null; set -f; PYTHONPATH=.claude/lib uv run python -m rumil_skills.trace $ARGUMENTS
```
