---
name: rumil-run
description: Run the rumil orchestrator against an existing question — a full multi-call research loop with a budget. This is the CC-initiated equivalent of `main.py --continue <qid> --budget N`. Use when the user wants real research done on a question, not just a single call. For one targeted call, use /rumil-dispatch instead. Budget defaults to 10; since that's not cheap, confirm with the user before firing if they didn't specify. Trigger when the user says things like "investigate this more", "run some research on this", "give Q# N calls of budget", or right after /rumil-ask when they want to immediately start investigating.
allowed-tools: Bash
argument-hint: "<question_id> [--budget N] [--smoke-test]"
---

# rumil-run

Fires the rumil orchestrator against an existing question with a budget.
This is the CC-initiated equivalent of `main.py --continue <id> --budget N`.
The orchestrator dispatches a *sequence* of calls (prioritize, scout,
find-considerations, assess, etc.) based on the active
`prioritizer_variant` setting and what the workspace needs, until the
budget is consumed or the orchestrator decides the question is done.

## When to use this vs. /rumil-dispatch

| | /rumil-dispatch | /rumil-run |
|---|---|---|
| **Unit** | one call of a specific type | the orchestrator (many calls) |
| **Budget default** | 3 (mostly for prioritize) | 10 |
| **Staged** | yes (sandbox by default) | no (visible in baseline workspace) |
| **Use when** | the user names a specific call type | the user wants real research progress |

Examples:
- "assess this question" → `/rumil-dispatch assess <id>`
- "find more considerations for this" → `/rumil-dispatch find-considerations <id>`
- "investigate this more" / "run research on this" → `/rumil-run <id>`
- "give this 10 more calls of budget" → `/rumil-run <id> --budget 10`

## When the model should invoke this directly

You should call this skill without explicit `/rumil-run` when the user's
intent is clearly "do real research on this question":

- "investigate this more" / "dig into this" / "run research on Q# abc12345"
- Right after `/rumil-ask`, if the user said "add and investigate X"
- "continue the research on this question with budget N"

**Confirm before firing if budget > 5** and the user didn't specify a
number. A default budget-10 run can take several minutes and cost real
money. One-line check: "Run the orchestrator on `abc12345` with budget
10? That'll fire 10 research calls."

## Defaults

- **Budget**: 10. High-cost compared to dispatch. Always confirm when
  the user didn't ask for a specific number.
- **Workspace**: inherited from session state. Override with `--workspace`.
- **Staged**: no — output is immediately visible to the frontend and
  other readers, the same way `main.py` would leave it. This is
  deliberately different from `/rumil-dispatch`, which runs staged
  (sandbox) by default.
- **Origin tag**: `origin=claude-code`, `skill=rumil-run`, captured in
  `runs.config` so later analyses can filter cc-initiated runs from
  `main.py` runs.

## Arguments

- **`<question_id>`** (positional, required): full UUID or short 8-char ID.
  Must be an existing question in the active workspace.
- **`--budget N`**: research-call budget. Default 10.
- **`--smoke-test`**: use Haiku and cap rounds — for fast, cheap testing.
- **`--workspace <name>`**: override the session's active workspace.
- **`--name <text>`**: optional run name; defaults to the question headline.

## Invocation

```!
PYTHONPATH=.claude/lib uv run python -m rumil_skills.run_orchestrator $ARGUMENTS
```

## After it runs

The orchestrator can run for many minutes. The script streams:
- workspace + question headline
- trace URL — **surface this immediately** so the user can watch progress
  in the browser alongside the CC session
- `→ running orchestrator (budget N)` confirmation line
- `✓ done: budget=used/total` completion line

### Natural next steps to offer

- **See what changed:** `/rumil-show <id>` — refreshed subtree view
- **Read the research:** `/rumil-review <id>` — structured punch list
- **Debug something that looked off:** `/rumil-find-confusion` — scan
  recent calls for model confusion; follow up with `/rumil-trace` on
  the top candidate
- **Iterate on prompts if a call misbehaved:** `/rumil-prompt-edit <call_id>`
