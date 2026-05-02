---
name: rumil-trace
description: Dump a rumil call's full execution trace — trace events plus every LLM exchange verbatim (system prompt, user message, response, tool calls). Use to inspect what happened inside a specific call, debug model confusion, or review a run for quality. Takes a short (8-char) or full call ID.
allowed-tools: Bash
argument-hint: "<call_id> [--brief] [--only llm_exchange] [--last-n N] [--system-once] [--user-only N] [--response-only N]"
---

# rumil-trace

Loads a call's full trace into the Claude Code conversation. By default
every LLM exchange is printed verbatim so you (and Claude) can see the
model's actual words — summaries lose the signal needed to spot
confusion.

Use filters for large traces:
- `--brief` shortens bodies and hides the system prompt
- `--only <event>` shows only events of one type (e.g. `llm_exchange`, `error`)
- `--last-n N` trims to the final N exchanges
- `--no-exchanges` shows just the event timeline
- `--system-once` prints the system prompt only on its first occurrence; subsequent
  identical prompts are replaced with `(system prompt unchanged from exchange N)`.
  Cuts ~60-70% off multi-round-call output where the system prompt repeats verbatim.
- `--user-only N` for each exchange, render only the last N chars of the user
  message (prefixed with `... (truncated, M chars total)` if trimmed).
- `--response-only N` same shape, applied to the assistant response.

These flags combine — e.g. `--system-once --user-only 2000 --response-only 2000`
gives a compact read of a long multi-round call with the high-signal tail of
each exchange and no system-prompt repetition.

```!
setopt no_glob 2>/dev/null; set -f; PYTHONPATH=.claude/lib uv run python -m rumil_skills.trace $ARGUMENTS
```
