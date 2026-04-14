---
name: rumil-review
description: Structured review of all research on a rumil question. Loads the subtree, all recent calls targeting the question (with brief traces), and any existing confusion-scan verdicts, then asks Claude to produce a ranked punch list of problems worth fixing — with per-item attribution and a suggested action (dispatch, apply_move, edit_prompt, or ignore). Use when the user wants to audit a question's research, not just look at individual calls.
argument-hint: "<question_id>"
---

# rumil-review

Loads a full review context for one question: its subtree, shape
diagnostics (structural health, rating distributions, self-reported
call signals), every recent call that targeted it (with compact trace
summaries), and any existing confusion-scan verdicts from the scan log.
Then you produce a punch list.

## Context loading

```!
PYTHONPATH=.claude/lib uv run python -m rumil_skills.gather_review_context $ARGUMENTS
```

## Your job

Read the loaded context carefully — subtree first (to understand what
the research is actually *about*), then the shape diagnostics (to
understand the structural and distributional health), then the recent
calls in reverse-chronological order (most recent first). Produce a
**structured punch list** of problems worth fixing.

The shape diagnostics section contains auto-detected findings from
three scans: graph health (structural topology problems), rating shape
(credence/robustness distribution issues), and review signals
(aggregated self-reports from calls). Each finding has a severity and
suggested action. Incorporate relevant findings into your punch list —
don't just repeat them, but use them as evidence alongside your own
reading of the subtree and calls. You may disagree with or downgrade
a finding if the full context warrants it.

Format each punch list item as:

```
[severity 1-5] <short_id>  <what's wrong in one sentence>
  → suggested: <action>
```

Where `<action>` is one of:

- `dispatch <call_type>` — a rumil call should be run to address this
  (e.g. `dispatch assess` to replace a stale judgement)
- `apply_move <MOVE_TYPE>` — a cc-mediated move would fix this directly
  (e.g. `apply_move CREATE_QUESTION` to add a missing sub-question, or
  `apply_move FLAG_FUNNINESS` to flag a page for human review)
- `edit_prompt <filename>` — the prompt the producing call used looks
  like the root cause; it should be edited. Identify the specific file
  in `prompts/`.
- `inspect` — a human should read the full trace; the problem is real
  but not clearly actionable
- `ignore` — borderline; flagged for completeness but not worth
  chasing

After the punch list, add a short **shape-level summary** (2-4 bullets):
patterns that cut across multiple items. Examples: "most find_considerations
calls on this question are producing thin output — suggests the task
prompt needs strengthening", or "the assess judgement is stale relative
to three considerations created after it".

## Guidance

- **Be specific.** Every item must cite a concrete page or call by
  short ID. Don't produce general observations without attribution.
- **Always summarize what an ID refers to.** When you cite `be6d1a1d`,
  write `be6d1a1d (the AI-governance-determines-space-allocation claim)`,
  not just the bare hex. Short IDs mean nothing without a quick gloss;
  forcing the user to look them up slows every discussion. Applies to
  page IDs, call IDs, and any other workspace entity.
- **Rank ruthlessly.** If you find 15 issues, put the three that
  actually matter at the top. Severity 5 should be rare — reserve it
  for problems that invalidate research output.
- **Favor small fixes.** A single `apply_move` is cheaper than a full
  re-dispatch, which is cheaper than editing a prompt and re-running
  everything. Suggest the smallest fix that clears the issue.
- **Don't dispatch or apply anything yourself.** This skill is
  read-only. The user picks what to act on from your punch list and
  fires the relevant skills manually.
- **If the confusion-scan verdicts are present**, treat them as prior
  evidence but don't blindly defer to them — you have the subtree
  context they didn't. Disagree when warranted and say why.

## Follow-ups the user may ask for

- "trace the top one" → suggest `/rumil-trace <call_id>`
- "let's fix it" → suggest `/rumil-clean <question_id>` for a guided
  accreting-only cleanup, or individual `/rumil-dispatch` calls for
  re-runs. For prompt-level fixes, pull the trace with `/rumil-trace
  <call_id>` and edit the relevant `prompts/*.md` directly.
