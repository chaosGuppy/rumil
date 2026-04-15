---
name: rumil-dispatch
description: Fire one rumil call (find_considerations, assess, scout-*, web_research, prioritize, create_view) as Claude Code. This is the "rumil-mediated" lane — a normal rumil call with its usual context-building, prompts, and tools. Use when the user wants to investigate a question deeper, assess a judgement, run a specific scout, or produce a distilled view summary. The run is tagged with origin=claude-code in the trace.
allowed-tools: Bash
argument-hint: "<call_type> <question_id> [--budget N] [--smoke-test]"
---

# rumil-dispatch

Fires one rumil call and streams terse progress into the conversation.
This is the **rumil-mediated lane**: the call is a standard rumil call
with its own context-building and prompts — Claude Code is just the
trigger. Distinguishable from a `main.py`-initiated run via the
`origin=claude-code` tag in `runs.config` and `calls.call_params`.

## Defaults

- **Budget**: `--budget 3` by default. Match this to the work you're asking
  for — single assess/scout calls don't need more. If the user wants a full
  multi-call investigation, suggest they use `main.py --continue` instead,
  which the orchestrator can spread budget across.
- **Workspace**: reads active workspace from `.claude/state/rumil-session.json`.
  Override with `--workspace <name>` if needed.
- **Visibility**: the script prints the trace URL immediately. Surface it to
  the user in your response so they can open it in a browser alongside the
  CC session.

## Call type picking

If the user asked for a specific type, use it. Otherwise:
- "investigate X more" / "find considerations" → `find-considerations`
- "assess X" / "judge X" / "how credible" → `assess`
- "brainstorm subquestions" → `scout-subquestions`
- "look for evidence on the web" → `web-research`
- "what should we work on next" → `prioritize`
- "summarize what we know" / "distill a view on X" → `create-view` (produces or updates a curated View summary page for a question; appropriate once a question has enough research to warrant a distilled summary)

When in doubt, ask the user before firing. Dispatching is not free.

## Invocation

```!
setopt no_glob 2>/dev/null; set -f; PYTHONPATH=.claude/lib uv run python -m rumil_skills.dispatch_call $ARGUMENTS
```

## After it runs

The script prints:
- trace URL (surface this)
- `→ firing <call_type> (budget N)` — confirmation line
- `✓ done: status=... cost=$... budget=used/total` — completion line
- the call's `result_summary` if non-empty

If the call failed or the result looks off, suggest using `/rumil-trace <call_id>`
to inspect what happened. The short call ID is in the trace URL.
