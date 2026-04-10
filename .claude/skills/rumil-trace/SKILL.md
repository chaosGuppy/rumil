---
name: rumil-trace
description: Dump a rumil call's full execution trace — trace events plus every LLM exchange verbatim (system prompt, user message, response, tool calls). Use to inspect what happened inside a specific call, debug model confusion, or review a run for quality. Takes a short (8-char) or full call ID.
allowed-tools: Bash
argument-hint: "<call_id> [--brief] [--only llm_exchange] [--last-n N]"
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

```!
PYTHONPATH=.claude/lib uv run python -m rumil_skills.trace $ARGUMENTS
```
