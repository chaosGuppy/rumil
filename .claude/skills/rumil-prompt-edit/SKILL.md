---
name: rumil-prompt-edit
description: Trace-driven editor for rumil prompt files. Takes a call ID, loads the full trace (events + LLM exchanges verbatim), identifies the prompt file(s) that call used, reads them, and helps the user spot and fix the prompt issue that led to the call's behavior. Use when rumil-find-confusion or rumil-review points at a specific call as the root cause of some problem and you want to fix the underlying prompt.
argument-hint: "<call_id>"
---

# rumil-prompt-edit

Takes a call that went sideways, reads its trace and the prompt it
ran against, helps you spot and fix the issue in the prompt. No new
script logic — this is mostly instructions for Claude to follow a
specific sequence of steps using tools it already has.

## Step 1 — Load the trace

```!
PYTHONPATH=.claude/lib uv run python -m rumil_skills.trace $ARGUMENTS
```

Read it carefully. The `!` block above runs `trace.py` which dumps
the full trace including every LLM exchange's system prompt, user
message, and response verbatim — you need the model's actual words
to see where it went wrong.

## Step 2 — Identify the prompt file(s)

rumil's prompts live in `prompts/`. Each call type has a main prompt
file. The shared preamble is `prompts/preamble.md` and is loaded for
all call types. The mapping (approximate — read `src/rumil/llm.py`
`build_system_prompt` if in doubt):

| call_type | main prompt file |
|-----------|------------------|
| find_considerations | `prompts/find_considerations.md` |
| assess | `prompts/assess.md` or `prompts/big_assess.md` (per `settings.assess_call_variant`) |
| prioritization | `prompts/prioritization.md` |
| scout_subquestions | `prompts/scout_subquestions.md` |
| scout_estimates | `prompts/scout_estimates.md` |
| scout_hypotheses | `prompts/scout_hypotheses.md` |
| scout_analogies | `prompts/scout_analogies.md` |
| scout_paradigm_cases | `prompts/scout_paradigm_cases.md` |
| scout_factchecks | `prompts/scout_factchecks.md` |
| scout_web_questions | `prompts/scout_web_questions.md` |
| scout_deep_questions | `prompts/scout_deep_questions.md` |
| scout_c_how_true | `prompts/scout_c_how_true.md` |
| scout_c_how_false | `prompts/scout_c_how_false.md` |
| scout_c_cruxes | `prompts/scout_c_cruxes.md` |
| scout_c_relevant_evidence | `prompts/scout_c_relevant_evidence.md` |
| scout_c_stress_test_cases | `prompts/scout_c_stress_test_cases.md` |
| scout_c_robustify | `prompts/scout_c_robustify.md` |
| scout_c_strengthen | `prompts/scout_c_strengthen.md` |
| web_research | `prompts/web_research.md` |
| ingest | `prompts/ingest.md` |
| evaluate | `prompts/evaluate.md` / `eval-falsifiable-grounding.md` / `eval-feedback.md` |
| summarize | `prompts/summarize.md` (or report-related files) |
| link_subquestions | `prompts/scope_subquestion_linker.md` |
| claim_investigation | `prompts/claim_investigation_p1.md` / `p2.md` |

Read `prompts/preamble.md` too — a surprising number of "the model
got confused about workspace concepts" issues originate there.

## Step 3 — Diagnose

Think specifically about what you saw in the trace. Don't guess at
root causes — cite what the model actually said. Questions to ask:

- Did the model misread an instruction? Which instruction, and is it
  ambiguous or buried?
- Did it ignore a rule? Is the rule stated clearly enough, or only
  implied?
- Did it lose track of the scope? Is the scope prominent in the
  context or buried under boilerplate?
- Did it misuse a tool? Is the tool description accurate?
- Did it produce thin output? Does the prompt set a bar for output
  depth, or does it make thin output feel acceptable?

State your diagnosis in one paragraph before proposing any edit.

## Step 4 — Propose an edit

Use the Edit tool to propose specific changes. Show the user the diff
via your edit call (or just describe what you're changing and why).
Keep edits surgical — this is a prompt fix, not a rewrite. If a full
rewrite feels warranted, say so and stop; let the user decide whether
to take that on.

## Step 5 — Suggest a re-dispatch

After the edit lands, the natural next step is re-dispatching the
same call type against the same question to see whether the new
prompt behaves. Use `/rumil-dispatch <call_type> <question_id>` or
suggest the exact command. With `runs.config.git_head` captured
automatically, the new run's trace will be correlated to the fix
commit for future review.

## Guardrails

- **Never edit `src/rumil/` code from this skill.** If the root cause
  is in Python (context builder, move logic, etc.), stop and say so;
  the user handles code fixes separately.
- **Commit the prompt change separately.** The repo convention is
  separate commits for unrelated changes, and a prompt fix is a
  discrete unit. Use the `/commit` skill with a message that names
  the symptom observed in the trace.
- **Gloss IDs.** Every page or call you cite in the discussion should
  have a 3-8 word gloss in parens, not a bare hex short ID.
