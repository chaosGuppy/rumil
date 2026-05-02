---
name: rumil-versus-iterate
description: Iterate on versus's pipeline — review recent (or freshly-fired) completion / judging runs, identify concrete improvement opportunities, test them via /rumil-forks, and consolidate findings into a ranked punch list. Spawns parallel agents for trace investigation and fork experiments so wall-clock stays low. Use when the user wants to "review the recent versus runs and find improvements," "play with these traces," "see what we could do better in d&e / two_phase," or anytime after a versus generation/judging batch when it's worth turning the runs into actionable code/prompt changes. Default scope is one forethought essay × Sonnet × budget 4, but every input is overridable.
allowed-tools: Bash, Read, Write, Edit, Agent
argument-hint: "[--fresh] [--workspace <name>] [--essay <essay_id>] [--model <model_id>] [--budget N] [--scope completion|judge|both] [--max-forks N]"
---

# rumil-versus-iterate

A meta-skill: takes versus runs (existing or fresh), spawns parallel
trace investigators + fork experimenters, and consolidates findings
into a punch list of concrete improvements.

This skill orchestrates other skills (`rumil-versus-generate`,
`rumil-versus-complete`, `rumil-versus-judge`, `rumil-trace`,
`rumil-forks`) and parallel `Agent` calls. The actual work happens in
those tools; this skill is the recipe.

## When to use

- "Review the recent versus runs, find improvements."
- "What's wrong with our two_phase orch on the judging side?"
- "Spin up some completions, look at what's going wrong, fork the bad
   exchanges, consolidate."
- After a fresh `rumil-versus-complete` or `rumil-versus-judge` batch
  when it's worth turning the runs into actionable changes.
- Whenever the user mentions trace inspection + forks + consolidation in
  the same breath.

## Defaults

When the user invokes this skill without specifying scope, assume:

- **Workspace**: `versus`
- **Essay**: one forethought essay (pick the most-recent or
  longest-completion among `versus_texts.essay_id LIKE 'forethought__%'`)
- **Model**: `claude-sonnet-4-6`
- **Budget**: 4
- **Scope**: both completion (TwoPhase + DraftAndEdit) and judge
  (TwoPhase orch)
- **Pair shape**: human baseline + at least 2 model continuations from
  different model families ("model-chooses-model" pairs) so the judge
  has a meaningful comparison
- **Fork samples**: 2 per experiment
- **Max forks**: 4 (tune up only if the user explicitly asks)

Always confirm scope back to the user in one line before firing fresh
runs (forks-on-existing-runs is cheap; fresh runs aren't).

## Phases

### Phase 0 — fresh runs (only if `--fresh` or runs are stale)

If `--fresh` is passed OR if the workspace has no recent runs matching
the requested scope, fire generation first:

1. **Single-shot completions** (cheap baseline contestants) via
   `rumil-versus-generate`:
   ```
   uv run python versus/scripts/run_completions.py \
       --workspace versus \
       --essay <essay_id> \
       --model <model_id> [--model <model2_id>...]
   ```

2. **Orch completions** via `rumil-versus-complete`:
   ```
   uv run python versus/scripts/run_completions.py \
       --workspace versus --staged \
       --orch two_phase --orch draft_and_edit \
       --essay <essay_id> --model <model_id> --budget <N>
   ```

3. **Judgments** via `rumil-versus-judge` — both blind and orch:
   ```
   # blind (cheap, ground truth for comparison)
   uv run python versus/scripts/run_judgments.py \
       --workspace versus --essay <essay_id> --judge-model <model_id>
   # orch (expensive, the thing we're auditing)
   uv run python versus/scripts/run_judgments.py \
       --workspace versus --staged --variant orch \
       --essay <essay_id> --judge-model <model_id> --budget <N>
   ```

Use `--staged` for orch runs so you don't pollute the canonical
`versus_texts` / `versus_judgments` rows during exploration. Confirm
total estimated $ before firing. A typical full-fresh round is
~$8-15 depending on model + essay length.

### Phase 1 — select representative runs

Query the workspace DB for runs in scope. The schema cheat sheet in
`rumil-system` covers column names; representative selection:

```python
import asyncio
from rumil.database import DB

async def main():
    db = await DB.create(run_id="scratch", prod=False, staged=False)
    proj = await db.get_or_create_project("versus")
    db.project_id = proj.id
    res = await db._execute(
        db.client.table("runs").select("id,created_at,config")
        .eq("project_id", db.project_id)
        .order("created_at", desc=True).limit(40)
    )
    for r in res.data:
        cfg = r.get("config") or {}
        print(r["id"][:8], cfg.get("workflow"), cfg.get("task_name"),
              cfg.get("essay_id", "")[:25])

asyncio.run(main())
```

Pick **one run per (workflow × task_name)** combination in scope:

- `two_phase` × `complete_essay`
- `draft_and_edit` × `complete_essay`
- `two_phase` × `general_quality` (or whichever judge dimension)

Skip runs with 0 calls — those are dedup'd shells, not real work.
Verify by checking `calls.count` for each candidate.

### Phase 2 — spawn parallel trace investigators

For each selected run, spawn one `Agent` (background) with
`subagent_type=general-purpose` and a focused prompt. Template:

> You're investigating a versus **{workflow}** **{task}** run to find
> concrete improvement opportunities. Working dir: /Users/brian/code/rumil.
> Active rumil workspace: `versus`.
>
> The run is `{run_id}` ({essay_id}, model {model}, budget {budget}).
> It {has N calls / produced N words / etc.}:
> {bulleted list of (call_id_short, call_type, status, $cost, params.phase)}
>
> Your job: identify CONCRETE improvement opportunities. NOT generic advice.
> Look for:
>   - Wasted budget (calls producing thin output relative to cost)
>   - Bad prompting (system or user messages that confuse the model,
>     miscue scope, leak info, etc.)
>   - Tool-use mistakes (wrong tool, looping, ignoring outputs)
>   - Output that fails the downstream consumer (scout output the
>     closer can't use; view items that paraphrase claims; etc.)
>   - For completions: does the orch's research subgraph actually feed
>     the closer's continuation, or does it ignore it?
>   - For judging: does the orch research inform the verdict, or could
>     a blind judge land the same call?
>   - Blind-leak risks (model speculating about source/human/AI)
>
> Tools:
>   - `PYTHONPATH=.claude/lib uv run python -m rumil_skills.trace <call_id>`
>     dumps trace events + verbatim LLM exchanges.
>   - `PYTHONPATH=.claude/lib uv run python -m rumil_skills.show_question <qid>`
>     for the resulting subgraph.
>   - For d&e workflows: trace events on the single VERSUS_COMPLETE call
>     hold drafter/critic/editor exchanges; see
>     `src/rumil/orchestrators/draft_and_edit.py`.
>   - The trace will be large; be selective — read prioritization +
>     a couple of scouts + the closer; don't read every exchange.
>
> Return:
>   1. Top 3 concrete improvements ranked by expected impact, each
>      with: (a) what's wrong, (b) where (cite call short-id +
>      brief excerpt), (c) specific fix (prompt edit, knob change,
>      code change).
>   2. One observation about whether orch research actually fed
>      downstream consumption.
>   3. Cost outliers worth flagging.
>
> Be terse. Under 500 words. Cite specific call ids and snippets.
> Don't speculate beyond evidence.

Run all trace agents in **a single message with multiple Agent
tool_use blocks** so they execute concurrently. Set
`run_in_background=true` so you can move on; you'll be notified when
each completes.

### Phase 3 — fork experiments

When trace findings come back, identify 1-3 fork-amenable hypotheses
PER RUN. Good fork hypotheses:

- "If we removed the View block from the closer's user message, would
  the verdict / continuation noticeably degrade?" (load-bearing test)
- "If the system prompt forbade paraphrase, would the model produce
  fewer denser items?" (prompt-tightening test)
- "If max_tokens was bumped to 32k, would the truncated edit complete?"
  (single-knob test)
- "If the user message included the actual essay opening (vs only a
  blurb), would the scout pick different cases?" (context test)

Skip hypotheses that don't fit forks — anything multi-turn, anything
that needs tool execution, anything that requires DB writes. Forks are
single-turn, side-effect-free.

Spawn one `Agent` per fork experiment, in parallel. Template:

> Run a rumil-forks experiment and report findings. Working dir:
> /Users/brian/code/rumil. Active workspace: `versus`.
>
> ## Hypothesis
> {one paragraph: what's wrong, what to test, expected signal}
>
> ## Steps
> 1. Dump the trace to find exchange_ids:
>    ```
>    PYTHONPATH=.claude/lib uv run python -m rumil_skills.trace <call_id>
>    ```
>    Pick the {round-1 / closer / editor / etc.} exchange.
> 2. `show` the exchange:
>    ```
>    uv run python scripts/exchange_forks.py show <exchange_id>
>    ```
> 3. Build overrides at `.scratch/forks/<descriptive_name>.json`
>    overriding ONLY {system_prompt | user_messages | max_tokens | model | etc.}.
>    {specific instructions for the override}
> 4. Fire 2 samples:
>    ```
>    uv run python scripts/exchange_forks.py fire <exchange_id> \
>        --overrides .scratch/forks/<name>.json --samples 2
>    ```
> 5. Report (under 350 words):
>    - {what to compare}
>    - Cost per sample.
>    - Honest call: does the variation strictly improve, strictly
>      degrade, or look mixed?
>
> ## Constraints
> - NO `--prod`.
> - DO NOT edit prompt files in `src/rumil/prompts/` or
>   `src/rumil/orchestrators/`.
> - DO NOT do extra unrelated forks.
> - Cite fork ids + cost.

Again: send all fork agent calls in **one message with multiple
Agent tool_uses** for concurrent execution.

### Phase 4 — consolidate

When all fork agents return, write a single ranked-improvements report
to the user. Structure:

- **P0 — fix immediately** (low effort, high signal, fork-confirmed)
- **P1 — worth doing** (clear value, may need broader sweep)
- **P2 — known-needed but not yet solved** (fork showed the obvious
  fix doesn't work; needs deeper change)
- **P3 — observation only** (interesting pattern, no clear action yet)

For each item:
- One-line statement of the problem
- Cite supporting trace finding + fork id
- Concrete fix (file path, prompt edit, knob change)
- Estimated impact ($ saved per run, quality delta, etc.)

End with a one-line offer to draft any of the P0 items as PRs.

## Where lessons land (the iteration target)

Not all parts of the system are equally OK to edit while iterating.
Be intentional about where wins from this loop get applied.

- **Completions: `DraftAndEditWorkflow` is the iteration target.**
  Lives at `src/rumil/orchestrators/draft_and_edit.py`. It's a
  versus-specific workflow whose drafter / N critics / editor loop
  does not touch the rest of rumil — change the prompts, the round
  structure, the editor's max_tokens, add an arbiter exchange, etc.,
  freely. Lessons from the trace+fork loop on **completion** runs
  should be applied here.
- **Completions: `TwoPhaseOrchestrator` is shared with normal rumil
  runs — leave its internals alone.** It runs research questions
  outside versus too, so changing prioritization / scout / view logic
  has spillover. Two ways to incorporate lessons without touching it:
    1. Change the **inputs** the versus harness gives it — Question
       framing, linked Source page, abstract, prefix surface
       (`versus/src/versus/tasks/complete_essay.py` is the right
       file).
    2. Change how the **closer** consumes its output — strip
       View from closer context, render claims differently, etc.
       (`render_for_closer` and friends).
- **Judging: there is no iteration-friendly judge workflow yet.**
  Currently versus judging has two paths — blind one-shot and
  `TwoPhaseOrchestrator --variant orch`. Same constraint as above:
  two_phase is shared with rumil, don't fiddle. **TODO**: add a
  versus-specific judging workflow (analogous to DraftAndEdit on the
  completion side) so lessons from the trace+fork loop on **judging**
  runs have somewhere to land. Flag this to the user when they're
  iterating on judge runs and findings would otherwise want to change
  two_phase internals — the right move is "build the new judge
  workflow, then apply" rather than "patch two_phase."

If a finding really wants to change two_phase internals, escalate
explicitly: name the change, why it's load-bearing, what it spills
into for non-versus rumil callers, and let the user decide whether
to break the boundary or build the new workflow first.

## Don't overfit

The trace+fork loop is high-leverage but the sample size is tiny
(usually n=2 forks on a single pair / single run). Discipline:

- A fork win on one pair is a **hypothesis**, not a verified
  improvement. Before flipping a default or committing a prompt
  edit, sweep the variation across 3-5 pairs / dimensions and
  confirm the signal holds.
- **Bug fixes are fine to commit immediately** (e.g. editor
  max_tokens truncation cascading into empty drafts — that's a
  deterministic failure, n=1 is enough). **Behavior changes on
  prompts / structure** need broader confirmation.
- Findings that emerge from the same run that produced them are
  most at risk of overfitting. Try forks from a *different* run
  in the same scope before committing the fix as default.
- The consolidation report should explicitly tag each P0 / P1 item
  with `(n=N forks, M pairs)` so the evidence base is visible.
- It's OK to leave a P1 in "needs sweep" status — that's better
  than committing a fix that overfits.

## Caveats

- **Generalization**: most fork experiments are n=2 on a single
  pair/run. The signal is real but caveat in the report — sweeping
  across more pairs/dimensions before flipping defaults is wise for
  anything that changes core behavior.
- **Blind-leak risk**: when forking judge exchanges, never let the
  override reveal source/human/AI labels. Stripping orch context to
  "blind-only" is fine; injecting source-id metadata is not.
- **Don't promote prompts casually**: forks never edit
  `src/rumil/prompts/*.md` or orchestrator prompts. If a fork wins,
  surface it as a prompt-edit suggestion in the consolidation report;
  don't auto-apply.
- **Local-only**: every script should refuse `--prod` unless the user
  explicitly opts in. Forks and generation alike.
- **One agent per concern**: don't bundle "trace investigate AND fork"
  into one agent prompt — the agent will short-circuit. Keep phases 2
  and 3 cleanly separated.
- **Don't expand orch internals** unless asked. The user may say
  "we use two_phase for normal rumil runs, don't fiddle." Default
  to fixing inputs to the orch and the closer's consumption of orch
  output, not the orch's traversal logic itself.

## Quick check (skill is working)

A successful end-to-end run produces:
- 2-3 trace agent reports, each citing 3+ specific call ids
- 4-6 fork agent reports, each with fork ids + cost
- A single consolidated punch list with P0/P1/P2/P3 sections
- Total wall-clock: 5-15 min depending on fork count and trace size
- Total $: $0.50-3 for forks (no fresh-runs); $5-15 if `--fresh`

If you're under 30 seconds wall-clock, you're not actually waiting
for agents — you skipped Phase 2 or 3.
