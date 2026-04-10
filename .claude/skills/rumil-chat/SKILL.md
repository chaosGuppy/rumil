---
name: rumil-chat
description: Chat with the user about a specific rumil question, with the question's subtree and embedding-based workspace neighbors loaded into Claude Code context. Use when the user wants to discuss a question, think through its research, spot problems, or plan next steps. If the user names a question ID (short or full), pass it as the argument.
argument-hint: "<question_id>"
---

# rumil-chat

Opens a Claude Code conversation focused on a specific rumil question.
Unlike rumil's internal chat (`main.py --chat`), **Claude Code is the
chat partner** — you have access to the full repo, the prompts, the
code, the database, *and* the loaded rumil context, all in the same
conversation.

## What's loaded

The `!` block below runs `show_question` to pull everything you need
into context at skill load time:

- Workspace name + active question
- Full research subtree (sub-questions, claims, judgements)
- Embedding-based neighbors from the rest of the workspace
- Recent calls that have targeted this question

After it runs, you already have every piece of context a normal rumil
chat would have — plus the whole repo.

## Loading the question

```!
PYTHONPATH=.claude/lib uv run python -m rumil_skills.show_question $ARGUMENTS --depth 3
```

## How to chat

Your job in this conversation is to help the user think about the
research. Not to race to produce outputs. Specifically:

- **Understand first.** Read the loaded subtree before answering. Notice
  what's there, what isn't, where claims feel thin.
- **Name specifics, not generalities.** When you reference a claim or
  question, use its short ID so the user can jump to it.
- **Gloss every ID.** When citing `be6d1a1d` (or any short ID), add a
  3-8 word summary in parens: `be6d1a1d (the space-as-proxy-for-AI-power
  claim)`. Bare hex forces the user to look it up. Do this for every
  ID reference in your replies, not just the first.
- **Ask before dispatching.** If you think a call would help, say so and
  propose it — don't fire `/rumil-dispatch` without confirmation. One call
  costs real money.
- **Use the two lanes deliberately.**
  - If the user decides they want a *structured* investigation (more
    considerations, a proper assessment), use `/rumil-dispatch` — that's
    the rumil-mediated lane.
  - If the user wants to *directly* add a subquestion, link two pages,
    flag a page, or create a claim from the conversation, use
    `/rumil-apply-move` semantics via the `apply_move.py` helper — that's
    the cc-mediated lane (see below). These mutations go onto a
    `CLAUDE_CODE_DIRECT` envelope Call so the trace is unambiguous.

## Making direct moves from CC context

If the conversation lands on a specific mutation the user wants (e.g.
"add a subquestion about X" or "link these two claims"), apply it via:

```bash
PYTHONPATH=.claude/lib uv run python -m rumil_skills.apply_move <MOVE_TYPE> '<payload_json>'
```

First time this runs in a session it creates a `CLAUDE_CODE_DIRECT`
envelope Call automatically; subsequent moves hang off the same envelope.
The trace URL is printed on every apply — surface it to the user.

Use `apply_move --list` to see the move types. Most common in chat:
- `CREATE_QUESTION` — create a new question page
- `LINK_CHILD_QUESTION` — link a question as a sub-question of another
- `LINK_CONSIDERATION` — link a claim as a consideration on a question
- `FLAG_FUNNINESS` — flag something that seems off
- `REPORT_DUPLICATE` — mark two pages as duplicates

## Inspecting or switching

- `/rumil-trace <call_id>` — if the user wants to look into a specific
  past call surfaced in the "recent calls" list.
- `/rumil-show <other_qid>` — switch focus to a different question.
- `/rumil-search <query>` — look for related pages anywhere in the
  workspace.
- `/rumil-workspace` — see/switch workspace.

## Provenance to keep in mind

This whole conversation operates in the **cc-mediated** lane by default.
Claude Code is acting from its own context, not from a rumil-internal
prompt, and every workspace mutation made via `apply_move` is tagged as
such at the database and trace level. Be honest in conversation about
which lane you're pulling from — rumil-dispatch runs produce pages and
claims that went through rumil's full prompt machinery; apply_move
mutations were your own call from a broader context.
