---
name: rumil-logs
description: Browse the llm_boundary_exchanges log — every Anthropic API request/response in the workspace, regardless of caller (chat, dispatch, orchestrator, structured_call). Default mode prints a compact table of recent exchanges; --full <id> dumps the verbatim request and response for one row. Use to audit what the model actually saw and said, debug hangs/errors at the API boundary, or review cost / latency / token usage across runs. Filters by source, model, run, call, time, and errors-only.
allowed-tools: Bash
argument-hint: "[--ws <name>] [--source <prefix>] [--model <substr>] [--run <id>] [--call <id>] [--since 30m|2h|1d] [--recent N] [--error-only] [--cross-ws] [--full <id>]"
---

# rumil-logs

> **Under the hood:** queries `llm_boundary_exchanges` (the transport-level
> log written by `rumil.observability.llm_boundary` from every Anthropic
> SDK call site). Independent of `call_llm_exchanges` / the trace UI.

Default = compact table of the 20 most recent exchanges in the active
workspace. Each row shows id, started, source, model, latency_ms, usage
tokens, and stop_reason (or error class).

## Common patterns

```!
setopt no_glob 2>/dev/null; set -f; PYTHONPATH=.claude/lib uv run python -m rumil_skills.llm_logs $ARGUMENTS
```

### Filter by source

`source` is the call-site label written by the boundary logger. Common values:

- `chat.handle_chat` / `chat.handle_chat_stream` — parma chat turns
- `llm.call_api` — every wrapped Anthropic `messages.create` (call types, orchestrator helpers, etc.)
- `llm.structured_call_parse` — `messages.parse` for structured outputs

Pass a prefix: `--source chat` matches both chat sources.

### Time and volume

- `--since 30m|2h|1d` cuts to recent activity
- `--recent N` raises the row cap (default 20)
- `--error-only` shows only rows with a recorded `error_class`

### Drill into one exchange

`--full <id-prefix>` dumps the full `request_json` (model, system, messages,
tools, etc.) and `response_json` for the matching row. The Anthropic API
key is **never** in the body (lives in the Authorization header) so this
is safe to share, but treat the rest as full conversation content.

### Cross-workspace

`--cross-ws` skips the project filter and shows boundary rows across all
workspaces. Useful when triaging a stuck run whose workspace you forget.

## Notes

- Boundary rows are written for every API exchange regardless of staged-ness
  — they record real network/billing events, not workspace state.
- Failures to insert a boundary row never crash the originating API call;
  if the table is missing or unreachable, the call goes through and a
  warning lands in stderr.
