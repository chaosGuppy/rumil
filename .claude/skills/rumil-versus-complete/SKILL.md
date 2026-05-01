---
name: rumil-versus-complete
description: Run orchestrator-driven essay completions for the versus pairwise-eval pipeline. Per essay × prefix × model × workflow, fires a rumil orchestrator (TwoPhase, DraftAndEdit) and writes the resulting continuation as a `versus_texts` row tagged `kind="completion"`, `source_id="orch:<workflow>:<model>:c<hash8>"` — pickable as a contestant by `rumil-versus-judge` afterwards. Use when the user wants to A/B orchestrator-produced continuations against single-shot model continuations or against the human baseline. Both `--orch two_phase` and `--orch draft_and_edit` are usable. For single-shot completions (no orch), use `rumil-versus-generate`.
allowed-tools: Bash, Read
argument-hint: "--orch <workflow_name> [--workspace <name>] [--model opus|sonnet|haiku|<full-id> ...] [--budget N] [--essay <id>...] [--prefix-label <id>] [--include-stale] [--limit N] [--concurrency N] [--persist] [--prod] [--workflow-arg key=value ...] [--dry-run]"
---

# rumil-versus-complete

Runs orchestrator-driven essay completions and writes them as
`versus_texts` rows that can be picked up as contestants by
`rumil-versus-judge`. The orch path runs a rumil workflow against a
per-essay Question (the essay opening + "continue this") and emits a
finished continuation. For workflows with `produces_artifact=False`
(e.g. `two_phase`) the runner fires a closing LLM call that reads
the workflow's research and writes the continuation. For
`produces_artifact=True` workflows (e.g. `draft_and_edit`) the
workflow writes the final text directly to `question.content` and
the runner reads it verbatim — no closer.

## Status

Both registered workflows are live on `versus-orch-refactor`:

- `--orch two_phase` (#426) — runs `TwoPhaseWorkflow` per essay,
  closer extracts the continuation. Cost numbers below are rough but
  grounded in real runs.
- `--orch draft_and_edit` (#427) — runs `DraftAndEditWorkflow`
  (drafter → N parallel critics → editor, repeated). No real cost
  measurements yet; the table below is best-effort and should be
  replaced once the first runs land.

Use `rumil-versus-generate` for the single-shot path.

## How it relates to other skills

| Want to... | Use |
|---|---|
| Single-shot completion (one LLM call, no orch) | `rumil-versus-generate` |
| Orch-driven completion (workflow + budget) | this skill |
| Judge resulting completions against each other | `rumil-versus-judge` |

Single-shot completions and orch completions both land in
`versus_texts`; they're distinguished by `source_id`. Single-shot
uses the bare model id (e.g. `claude-opus-4-7`); orch uses
`orch:<workflow>:<model>:c<hash8>` (see "Source id" below). Both can
be paired against each other and against the human baseline by
`rumil-versus-judge`.

## Source id

Each orch completion lands with
`source_id = orch:<workflow_name>:<model>:c<hash8>` where:

- `<workflow_name>` is the workflow's stable id (`two_phase`,
  `draft_and_edit`, `experimental`, ...).
- `<model>` is the model passed via `--model`. For workflows that
  emit through a closer (e.g. `two_phase`) it's the closer's model;
  for workflows that produce the artifact directly
  (e.g. `draft_and_edit`, `produces_artifact=True`) it's the default
  model for every role unless a per-role override
  (`--workflow-arg drafter_model=...` etc.) is set. Per-role overrides
  are recorded in `config_hash` even though only the default shows in
  `source_id`.
- `c<hash8>` is the first 8 hex chars of the workflow's
  `config_hash` — the dedup primitive. Pinning the config hash into
  `source_id` means budget=4 and budget=10 of the same workflow are
  separate contestants and can be paired against each other in
  judging. Different workflows under the same model are also separate
  contestants by design.

`request_hash` (already on `versus_texts`) is the row-level dedup
key — it forks on workflow / model / prompts / sampling and so re-runs
under the same effective config naturally dedup. Two rows with the
same `source_id` under one essay × prefix collapse to one contestant
in pair enumeration (last-row-wins), so it's safe to top up a single
config.

## When to use

| Intent | This skill? |
|---|---|
| "make orch:two_phase continuations on essay X" | yes |
| "compare draft_and_edit at budget 8 vs 16" | yes (different `c<hash8>` → pairable) |
| "run claude-opus-4-7 single-shot completions" | **no** — use `rumil-versus-generate` |
| "judge existing pairs" | **no** — use `rumil-versus-judge` |

## Before any run: check staleness

Same gate as the other versus skills:

```bash
cd /Users/brian/code/rumil && uv run python versus/scripts/status.py
```

Exit code 2 + `STALE` banner means existing rows reference OLD essay
text. Re-run `run_completions.py` (single-shot path) before topping
up orch completions, since orch completions reference the same
`prefix_hash` keys.

## Workspace requirement

`--workspace <name>` maps to a rumil Project — no default; the user
must name one. For the orch variant to do better than a fresh draft,
that workspace should have material relevant to the essays' topics
(matches the judge skill's expectation).

**Prod has a dedicated `versus` workspace** for orch runs. Pass
`--workspace versus --prod` to use it. New workspaces must be created
via rumil's `main.py` first — the resolver fails-loud on missing
names.

By default orch completions are **staged** (`staged=True` on rumil's
DB). Workflow scratchwork (intermediate drafts, critic outputs,
research considerations) is invisible to baseline readers of the
workspace. Pass `--persist` to write to the baseline. The final
completion text always lands in `versus_texts` regardless of staging
— staging only governs the rumil pages.

## Env & config

- `ANTHROPIC_API_KEY` resolves from `versus/.env`, then
  `<rumil-root>/.env`, then process env. claude-* workflows only need
  this. Workflows that route non-claude models go through OpenRouter
  and need `OPENROUTER_API_KEY` too.
- `--model` accepts a short alias (`opus` / `sonnet` / `haiku`), a
  bare Anthropic id, or an OpenRouter id. For workflows with multiple
  roles, `--model` sets the closer / final-emit model; per-role
  overrides go through `--workflow-arg drafter_model=... critic_model=...`
  (see workflow-specific docs).
- `--budget` — orchestrator research-call budget. Minimum varies by
  workflow:
  - `two_phase`: 4 (`MIN_TWOPHASE_BUDGET`)
  - `draft_and_edit`: 1 round draft-only; 2+ adds critic/edit cycles
  - `claim_investigation`, `experimental`: 4
- `--orch <workflow_name>` is required. No default — caller picks
  the workflow explicitly.

## Invocation

```!
cd /Users/brian/code/rumil && uv run --with httpx --with pydantic --with pyyaml versus/scripts/run_completions.py $ARGUMENTS
```

Versus has its own `pyproject.toml` and isn't installed in rumil's
`.venv`, so the runtime deps are passed via `uv run --with`.

Typical invocations (substitute the user's chosen workspace for `<WS>`):

- `--orch two_phase --workspace <WS> --budget 4 --model sonnet --dry-run` — list pending two_phase orch completions on the active essay set
- `--orch two_phase --workspace <WS> --budget 4 --model sonnet --limit 3` — run on 3 pending essays
- `--orch draft_and_edit --workspace <WS> --budget 4 --model opus --essay forethought__broad-timelines` — run draft-and-edit on one essay
- `--orch draft_and_edit --workspace <WS> --budget 8 --model opus --include-stale` — also runs on off-feed essays

The `--essay` and `--prefix-label` filters mirror `rumil-versus-generate`.
`--include-stale` is the same opt-out from the active-essay-set default.

## What to surface

- `[plan] N orch completions ...` — pending count. Use this before
  confirming cost.
- `[run] <trace_url>` — per-completion. Surface immediately so the
  user can follow along live (hard requirement, same as the judge
  skill).
- `[done i/N] <essay_id> <source_id> trace=<url>` — completion landed.
- `[err ] <key>: <msg>` — failure (run continues for other essays).

After a run completes, suggest topping up judgments with
`rumil-versus-judge` to actually evaluate the new contestants.

## Cost confirmation

**Always `--dry-run` first** and **confirm with the user before firing
if expected cost is > ~$10**.

Per-completion estimates. **Numbers are rough estimates.** `two_phase`
extrapolates from the per-judgment cost on the same orchestrator;
`draft_and_edit` has **no real measurements yet** — replace the table
once the first runs land.

| Workflow | Model | $/completion | 25 essays |
|---|---|---|---|
| two_phase, budget=4 | sonnet | ~$1-3 | $25-75 |
| two_phase, budget=4 | opus | ~$3-10 | $75-250 |
| draft_and_edit, budget=4 (1 round, 2 critics) | sonnet | ~$0.40-1.20 (est) | $10-30 (est) |
| draft_and_edit, budget=4 (1 round, 2 critics) | opus | ~$1-3 (est) | $25-75 (est) |
| draft_and_edit, budget=8 (3 rounds, 3 critics) | opus | ~$3-8 (est) | $75-200 (est) |

For expensive paths, always start with `--limit 3` and confirm actual
per-completion cost from the first results before scaling.

## Running long batches in the background

Same pattern as `rumil-versus-judge`. Single non-compound command,
fire with `run_in_background: true` from the start, redirect stdout
to a logfile:

```
uv run ... versus/scripts/run_completions.py --orch <workflow> ... > /tmp/versus-complete-<id>.log 2>&1
```

Watch progress by:
- `grep '^\[run\]' /tmp/versus-complete-<id>.log` — emit trace URLs as
  they appear (post each one to the user immediately).
- Querying `versus_texts` directly — counts increment regardless of
  stdout buffering.

## What forks `request_hash` (dedup)

Anything in the canonical request body the workflow + closer construct
flows into `versus_texts.request_hash`. So forking is automatic for:

- `--model` change → different model in the workflow / closer call
- `--budget` change → different `budget` in workflow fingerprint
- `--workflow-arg` changes (e.g. `n_critics`, `max_rounds`,
  `drafter_model`) → folded into the workflow's `fingerprint()` and
  fork the hash
- Edits to drafter / critic / editor prompts (for draft_and_edit) →
  prompt hashes fork
- Edits to workflow / closer / runner code under
  `SHARED_CODE_FINGERPRINT_DIRS` (harness layer) and the workflow's
  declared `code_paths` (per-workflow layer, post-#425) →
  `shared_code_fingerprint` and `workflow_code_fingerprint` fork
  independently
- Workflow's `relevant_settings` snapshot — see
  `_BudgetedOrchWorkflow.relevant_settings` for the full list (today:
  `assess_call_variant`, `available_moves`, `available_calls`,
  `enable_red_team`, `enable_global_prio`, `subquestion_linker_enabled`,
  `prioritizer_variant`, `view_variant`, `budget_pacing_enabled`).
  Flipping any of those forks naturally.
- Mutations to baseline workspace pages between runs →
  `workspace_state_hash` watermark bumps

If a surface isn't on this list and you suspect it should fork
`request_hash`, the right fix is to extend
`make_versus_config` (`versus/src/versus/versus_config.py`,
post-#424), not to add a manual version bump.

## Caveats

- **Each orch completion creates a rumil Run.** Each shows up on
  `/traces`. Use a low `--limit` initially.
- **Re-running is free.** `request_hash` covers all effective inputs;
  same effective config dedups against existing rows. Different
  configs land as new rows under the same `source_id` (the
  `:c<hash8>` slug forks naturally).
- **Rumil trace UI requires rumil's frontend** (`./scripts/dev-api.sh`
  + `cd frontend && pnpm dev`). Trace URLs point at
  `settings.frontend_url` (default http://127.0.0.1:3000).
- **Workflow-specific knobs** (e.g. `n_critics`, `max_rounds` for
  draft_and_edit) are passed via `--workflow-arg key=value`. See the
  workflow's class docstring for available knobs; they all fold into
  the workflow's `fingerprint()` and so fork `request_hash`
  naturally.
