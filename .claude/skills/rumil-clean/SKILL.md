---
name: rumil-clean
description: Guided cleanup of research on a rumil question. Two modes — (a) pipeline mode wraps rumil's existing grounding/feedback clean pipelines given an evaluate call id; (b) interactive mode (default) walks a punch list conversationally, proposes each mutation, talks it through with the user, and applies accreting-only moves via the chat envelope after explicit consent. Use when rumil-review has produced a punch list and the user wants to act on it safely.
argument-hint: "<question_id> | --pipeline grounding|feedback <eval_call_id>"
---

# rumil-clean

> **Under the hood:** Mode A (pipeline) calls
> `rumil_skills.run_clean_pipeline`, which routes through the shared
> `rumil.dispatch.dispatch_grounding_pipeline` function — the same
> dispatch that backs `POST /api/calls/{call_id}/ground` and `POST
> /api/calls/{call_id}/feedback` (and that main.py's `--ground` /
> `--feedback` flags are CLI equivalents of). The underlying runners
> are `rumil.clean.grounding.run_grounding_feedback` and
> `rumil.clean.feedback.run_feedback_update`, registered in
> `rumil.evaluate.registry.GROUNDING_PIPELINES`. Mode B (interactive) is
> cc-mediated and applies individual moves via `rumil_skills.apply_move`
> onto a `CLAUDE_CODE_DIRECT` envelope call; no dispatch function is
> involved.

Turns a review into applied fixes, safely. There's a fast lane and a
careful lane.

## Two modes

### Mode A — pipeline (rumil-internal)

When you already have a completed `evaluate` call on a question and
want rumil's own clean pipelines to run against its evaluation text,
this mode wraps them:

```bash
PYTHONPATH=.claude/lib uv run python -m rumil_skills.run_clean_pipeline \
    grounding <eval_call_id>

PYTHONPATH=.claude/lib uv run python -m rumil_skills.run_clean_pipeline \
    feedback <eval_call_id>
```

`grounding` runs `clean.grounding.run_grounding_feedback` — creates
source pages and grounds claims in real evidence.

`feedback` runs `clean.feedback.run_feedback_update` — applies proposed
changes from a feedback-style evaluation (new claims, new investigations,
link fixes).

Both are rumil-mediated: the actual mutations are chosen by a
rumil-internal LLM pipeline, not by you. You're just the trigger.
The run is tagged `origin=claude-code skill=rumil-clean pipeline=...`
and gets its own trace URL. Use this when you trust the rumil clean
pipeline to do the right thing and just want it to run.

**Prereq:** the user must have already run `/rumil-dispatch evaluate
<question_id>` to produce an EVALUATE call with evaluation text in its
`review_json`.

### Mode B — interactive (cc-mediated, accreting-only)

This is the conversational lane. The user typically lands here after
running `/rumil-review <question_id>` and getting back a punch list.
Your job is to walk the punch list one item at a time, proposing each
mutation, talking it through, and applying it after explicit user
consent. Strict rules:

1. **Accreting-only.** Every applied move goes through
   `apply_move.py --accreting-only`. The allowlist is: CREATE_* moves,
   non-destructive LINK_* moves, FLAG_FUNNINESS, REPORT_DUPLICATE,
   PROPOSE_CONCEPT, LOAD_PAGE. **Never** propose or apply REMOVE_LINK,
   UPDATE_EPISTEMIC, CHANGE_LINK_ROLE, or PROMOTE_CONCEPT from this
   skill — they modify or migrate existing state. If a punch-list item
   requires one of those, say so and suggest the user use the
   destructive tool manually or run the rumil-mediated clean pipeline
   instead.

2. **Talk through, then act.** Never apply a move without stating what
   you're about to do and giving the user a chance to object. Format:
   > "Proposing: `FLAG_FUNNINESS` on `be6d1a1d` (the
   > AI-governance-determines-space-allocation claim) — the headline
   > frames it as a claim but the body reads as a meta-reframe. OK to
   > apply?"

3. **Dry-run first for anything non-trivial.** For moves with complex
   payloads (links with reasoning, claims with long content), run
   `apply_move.py --dry-run` first so you can show the user what the
   validated payload looks like before committing.

4. **One move per turn.** Don't batch five mutations in a single
   response. Each move should be its own little propose-apply cycle
   so the user can stop or redirect anytime.

5. **Always gloss IDs.** When you cite a page or call, include a
   3-8 word summary in parens: `be6d1a1d (the claim about power-driven
   allocation)`. Never drop bare hex.

## Invocation

There's no single `!` block here because the skill dispatches based on
args. Parse `$ARGUMENTS` yourself:

- If the first arg is `--pipeline grounding|feedback` followed by an
  eval call id → run Mode A via `run_clean_pipeline`.
- If the first arg is a question ID → run Mode B: first load the
  question context via `gather_review_context`, then walk whatever
  punch list is already in the conversation (or ask the user for one).

If no punch list exists yet, suggest the user run `/rumil-review
<qid>` first, unless they explicitly want you to produce one here.

## What to show the user after each applied move

Just the one-line skill output:
```
⚙ cc-mediated move: <type>
• created page <short_id>
<one-line headline of result.message>
```

Plus (once per session) the envelope trace URL, so they can open the
rumil frontend to see the whole envelope Call and the moves flowing
into it.

## Escape hatches

- If the user changes their mind mid-session, stop applying and
  summarize what's been done + what's still pending from the punch
  list.
- If a proposed mutation would require destructive capability (edit
  an existing claim, remove a link, change a link role), say so and
  skip it — do not escalate to non-accreting moves. The user can
  handle those manually.
- If the user wants to switch to Mode A, just exit the conversational
  loop and fire `run_clean_pipeline` directly.
