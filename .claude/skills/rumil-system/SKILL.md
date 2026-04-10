---
name: rumil-system
description: Background knowledge for working with the rumil research workspace from Claude Code. Explains the two-lane provenance model (rumil-mediated vs cc-mediated), when to use which rumil-* skill, how workspace/session state works, and the visibility/attribution story. Auto-load this whenever the user asks about rumil, wants to inspect research, dispatch a call, chat with a question, apply moves, or review a trace.
user-invocable: false
---

# rumil-system — working with rumil from Claude Code

Rumil is an LLM-powered research workspace in this repo. Users pose
questions; rumil investigates them by dispatching structured calls that
produce pages (claims, questions, judgements, concepts) linked into a
research graph. See `CLAUDE.md` for the architecture.

This skill is background knowledge. It loads whenever the user is doing
rumil-related work in Claude Code so you don't need to rediscover these
patterns every time.

## Two lanes, clearly marked

Every workspace mutation made from Claude Code falls into one of two
lanes. **You must be clear which lane you're in** — both in your
conversation with the user and in the trace record.

### Rumil-mediated lane

A normal rumil call (`find_considerations`, `assess`, `scout_*`,
`web_research`, `prioritize`) fired via `/rumil-dispatch`. The call
goes through rumil's full pipeline: carefully-scoped context builder,
rumil prompts, rumil tools, bounded LLM agent loop. Claude Code is just
the trigger — the model inside the call sees a tight rumil prompt, not
the broader CC conversation.

**Tags:**
- `runs.config.origin = "claude-code"`
- `runs.config.skill = "rumil-dispatch"`
- `calls.call_params.origin = "claude-code"`
- `runs.name` is prefixed `[cc]`
- `runs.config.git_head` records the code state the run used

**Use when:** the user wants real research progress — more
considerations, a proper assessment, a scout pass, web research.

### CC-mediated lane

Claude Code is the brain. You (Claude) decide from your conversation
context — which is *much* broader than any rumil prompt — that a
specific move should happen. The move is applied directly via
`apply_move.py` onto a `CallType.CLAUDE_CODE_DIRECT` envelope Call.
There is *no* rumil-internal LLM call involved; the envelope exists
purely to give the move a well-defined owner in the trace.

**Tags:**
- Call type is `CLAUDE_CODE_DIRECT` (unique to this lane)
- `calls.call_params.origin = "claude-code"`
- `calls.call_params.envelope = true`

**Use when:** during `/rumil-chat` the user decides they want a
specific mutation right now (add a subquestion, link two pages, flag a
page, mark a duplicate) and there's no value in running a full rumil
call to mint it.

### Why the split matters

A future reviewer looking at a claim needs to know whether it came from
a careful rumil assess call or from a Claude Code conversation where
the context might have been sprawling. The CallType + call_params tags
make this unambiguous. Respect the split.

## The rumil-* skill surface

Direct skills (run scripts immediately, no LLM turn needed):

- `/rumil-workspace` — show/list/set the active workspace
- `/rumil-list` — list root questions in the active workspace
- `/rumil-show <qid>` — render a question's subtree, embedding
  neighbors, and recent calls
- `/rumil-search <query>` — embedding search over the workspace
- `/rumil-trace <call_id>` — dump a call's full trace and LLM exchanges
  verbatim

Model-mediated skills (you interpret intent, then act):

- `/rumil-dispatch <call_type> <qid>` — fire one rumil call
  (rumil-mediated lane)
- `/rumil-chat <qid>` — discuss a question with the user, can apply
  moves (cc-mediated lane)

## Session state

`.claude/state/rumil-session.json` holds:

- `workspace` — the active workspace for this CC session. Every skill
  defaults to this. Override per-call with `--workspace`.
- `chat_envelope` — the active CLAUDE_CODE_DIRECT envelope Call (if
  any). Used by `apply_move` to group cc-mediated mutations.

You can read this file directly if you need to know the current state.

## Running scripts directly

All skill scripts live in `.claude/lib/rumil_skills/` and can be run
outside their SKILL.md wrappers when needed:

```bash
PYTHONPATH=.claude/lib uv run python -m rumil_skills.<script_name> [args]
```

Scripts: `workspace`, `list_questions`, `show_question`, `search_workspace`,
`trace`, `dispatch_call`, `chat_envelope`, `apply_move`.

## Gloss page and call IDs

Whenever you cite a page, call, or any other rumil entity by its short
ID (8-char hex), include a brief gloss: `be6d1a1d (the
AI-governance-determines-space-allocation claim)`, not just `be6d1a1d`.
Bare IDs are opaque — forcing the user to switch to the frontend or
run another skill to know what you're pointing at slows every
discussion. Apply this across every rumil skill's output.

## Visibility and attribution

- **Trace URLs**: every script that creates a run prints the trace URL
  first. Surface it to the user so they can open the rumil frontend
  alongside CC.
- **Terse logging**: scripts print one line per significant event. When
  you relay skill output to the user, keep it scannable — don't paraphrase
  the trace URL away.
- **Git state**: `runs.config.git_head` captures the sha at invocation
  time, so later reviews can correlate a run to the exact code that
  produced it. Useful when iterating on prompts.
- **Local-only by default**: every script refuses `--prod` unless
  `RUMIL_ALLOW_PROD=1` is set in the shell. Don't try to bypass this.

## When NOT to use these skills

- For running a full orchestrator (multi-call investigation with
  prioritization), prefer `uv run python main.py --continue <qid>
  --budget N` — the orchestrator can spread budget across many calls,
  which `/rumil-dispatch` cannot.
- For bulk batch work, use `main.py --batch`.
- For A/B testing configs, use `scripts/run_call.py --ab` or
  `main.py --ab`.
