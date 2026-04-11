---
name: rumil-ask
description: Add a new research question to the active rumil workspace. Creates the question page via the cc-mediated envelope lane but does NOT run any research calls — chain with /rumil-orchestrate or /rumil-dispatch afterward to investigate. Use whenever the user wants to pose a new root question or add a sub-question (either explicitly via /rumil-ask or mid-conversation when they say things like "add a question about X" or "let's track a new question for X"). Pass the headline as the argument; optionally --parent <qid> for a subquestion, --abstract/--content for more detail.
allowed-tools: Bash
argument-hint: "<headline> [--parent <qid>] [--abstract \"...\"] [--content \"...\"]"
---

# rumil-ask

Adds a new research question to the active rumil workspace. This is the
**cc-mediated lane** for question creation: the new page is owned by the
current CC session's `CLAUDE_CODE_DIRECT` envelope Call, so the trace makes
the provenance unambiguous (same pattern as `apply_move CREATE_QUESTION`).

## What it does not do

- **Does not run any research calls.** This is just the ask. After it
  returns, chain with:
  - `/rumil-orchestrate <id> --budget N` — fire the full orchestrator
    (multi-call research loop)
  - `/rumil-dispatch <call_type> <id>` — fire one targeted call
- **Does not consume budget.** Creating a question is free.
- **Does not open a chat.** If the user wants a scoping conversation
  before committing to a wording, talk with them first in this CC session
  and call `/rumil-ask` once you've landed on the final headline.

## When the model should invoke this directly

You (the model) should call this skill without explicit `/rumil-ask`
invocation when the user clearly wants a new question in the workspace:

- "add a question about X" / "track a new question for X"
- "let's investigate whether Y" (pose the question first, then offer to run)
- "create a sub-question under Q#abc12345 about Z" (pass `--parent abc12345`)

When the user's intent is unclear — e.g. they're brainstorming and
haven't decided they want a persistent question — ask before committing.

## Arguments

- **`<headline>`** (positional, required): the question, phrased as a
  question (10-20 words). Or a path to a `.json` file with `headline`
  and optional `abstract` / `content` fields (same format `main.py`
  uses).
- **`--parent <qid>`**: attach as a sub-question of another question.
  Full or short 8-char ID. Without this, the new question is a root.
- **`--abstract "..."`**: 1-3 sentence summary. Used as the page's
  `abstract` field (which drives embedding-based search).
- **`--content "..."`**: longer description. Defaults to the headline
  if not set.
- **`--workspace <name>`**: override the session's active workspace.

## Invocation

```!
PYTHONPATH=.claude/lib uv run python -m rumil_skills.ask_question $ARGUMENTS
```

## After it runs

The script prints:
- `workspace: <name>` and envelope info
- trace URL — **surface this to the user** so they can watch the envelope
  fill up with CC-mediated moves
- `• created question <short_id>  <headline>`
- `• linked as child of <parent_id>` if `--parent` was set
- The full new question ID and a suggested `/rumil-orchestrate <short_id>`
  command as a hint

Always gloss the new ID in your reply the way the memory rule requires
(`abc12345 (the new question about X)`), not bare hex.

### Natural next steps to offer

- **Investigate now:** "want me to /rumil-orchestrate it? default budget 10"
- **Look at related workspace pages first:** `/rumil-search "<headline>"`
  to see if the workspace already has related material
- **Just add and move on:** acknowledge and continue the conversation
