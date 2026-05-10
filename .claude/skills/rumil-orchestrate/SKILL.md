---
name: rumil-orchestrate
description: Fire the rumil orchestrator against an existing question — a full multi-call research loop with a budget. This is the CC-initiated equivalent of `main.py --continue <qid> --budget N`. Use when the user wants real research done on a question, not just a single call. For one targeted call, use /rumil-dispatch instead. Budget defaults to 10; since that's not cheap, confirm with the user before firing if they didn't specify. Trigger when the user says things like "investigate this more", "run some research on this", "give Q# N calls of budget", or right after /rumil-ask when they want to immediately start investigating.
allowed-tools: Bash
argument-hint: "<question_id> [--budget N] [--orchestrator two_phase|experimental|simple_spine|axon] [--config <preset>] [--model <id>] [--no-compaction] [--global-prio|--no-global-prio] [--smoke-test]"
---

# rumil-orchestrate

Fires the rumil orchestrator against an existing question with a budget.
This is the CC-initiated equivalent of `main.py --continue <id> --budget N`.
The orchestrator dispatches a *sequence* of calls (prioritize, scout,
find-considerations, assess, etc.) based on the chosen orchestrator
variant and what the workspace needs, until the budget is consumed or
the orchestrator decides the question is done.

## Which orchestrator

Rumil ships four top-level research-loop orchestrators, selected via the
`--orchestrator` flag (or, equivalently, the `prioritizer_variant`
setting):

- **`two_phase`** (default) — `TwoPhaseOrchestrator`. The production
  loop: prioritizes sub-questions, dispatches per-call rounds, reviews
  judgements. What `main.py --continue` runs unless the settings are
  overridden. Budget unit: rumil calls.
- **`experimental`** — `ExperimentalOrchestrator`. Alternate
  prioritization / dispatch strategy (subquestion linker, per-call-type
  cost accounting). Use when the user is comparing variants or
  explicitly asks for it. Budget unit: rumil calls.
- **`simple_spine`** — `SimpleSpineOrchestrator`. Single persistent
  agent thread with parallel spawn subroutines (workspace_lookup,
  web_research, deep_dive). Budget unit: USD (`--budget <float>`),
  enforced via `pricing.compute_cost` so input + output + cache_create
  + cache_read all hit the cap at per-model rates. Driven by a YAML
  preset (`--config`) that bundles the library + an optional
  output_guidance / output_schema declaring the deliverable shape.
  Use for one-shot synthesis tasks where you want a single agent
  making the call sequence in-thread rather than a separate
  prioritization LLM.
- **`axon`** — `AxonOrchestrator`. Cache-aware mainline thread with a
  tiny fixed tool surface (`delegate`, `configure`, `finalize`,
  `load_page`) and two-step dispatch: each `delegate(intent,
  inherit_context, budget_usd, n)` is followed by a `configure`
  continuation that emits a structured `DelegateConfig` (system prompt,
  tools, finalize schema, side effects), then the orchestrator runs the
  delegate's inner loop and gathers all parallel results before the next
  mainline turn. Budget unit: USD (`--budget <float>`). Driven by a YAML
  preset (`--config`). Good for research questions where parallel
  sub-investigations + structured synthesis matter, and where keeping
  the spine cache prefix stable across many turns is worth the
  two-step dispatch overhead. **Note: axon currently has no `main.py`
  CLI hook and is not yet wired into the skill's `run_orchestrator.py`
  driver — invoke it via direct Python (instantiate `AxonOrchestrator`
  with an `AxonConfig` and `OrchInputs`). Wiring it up is a known
  follow-up.**

Not selectable here: `ClaimInvestigationOrchestrator` (a sub-orchestrator
used *inside* two_phase for per-claim work) and `RobustifyOrchestrator`
(the robustify call type, which is a `/rumil-dispatch robustify` concern,
not a research loop).

The chosen variant is captured in `runs.config.prioritizer_variant` so
later analyses can filter by orchestrator.

### SimpleSpine presets

Available `--config` values are the YAML files under
`src/rumil/orchestrators/simple_spine/configs/` (auto-discovered):

- **`research`** — workspace-grounded research with the three-subroutine
  library. No bundled deliverable shape; the agent's `finalize` answer
  is whatever it produces.
- **`view_freeform`** — same library plus a bundled four-section
  deliverable (`framing_and_interpretation`, `assertions_and_deductions`,
  `research_direction`, `returns_to_further_research`). Pick this when
  you want a view-shaped take on a question; structured_answer is
  populated automatically.
- `essay_continuation`, `judge_pair` — versus-eval-only presets, not
  useful for general research.

### Axon presets

Available `--config` values are the YAML files under
`src/rumil/orchestrators/axon/configs/` (auto-discovered):

- **`research`** — minimal mainline (delegate / configure / finalize /
  load_page) plus a system-prompt registry for `web_research` and
  `workspace_lookup` delegate flavors and a finalize-schema registry
  (`freeform_text`, `research_synthesis`). The spine decides per-turn
  whether to delegate, what intent to delegate with, and how many in
  parallel; configure picks the system prompt + tools + finalize shape.

### Global-prio (orthogonal)

`GlobalPrioOrchestrator` runs a cross-cutting global prioritiser
*concurrently* with the local (variant-selected) orchestrator. It
splits the remaining budget (default 20% global / 80% local, via
`global_prio_budget_fraction`) and `asyncio.gather`s both. It doesn't
replace the variant — it runs beside it. Gated by
`settings.enable_global_prio`, which comes from the `ENABLE_GLOBAL_PRIO`
env var / `.env` default (off by default). (Edge case: if the local
share falls below `MIN_TWOPHASE_BUDGET`, only the global loop runs.)

Use `--global-prio` or `--no-global-prio` to force it on/off for a
single invocation, overriding the env default. Omit the flag to inherit
whatever the env/settings say. The flag is tri-state: unset means
"inherit", `--global-prio` means "force on", `--no-global-prio` means
"force off" even if the env has it enabled.

## When to use this vs. /rumil-dispatch

| | /rumil-dispatch | /rumil-orchestrate |
|---|---|---|
| **Unit** | one call of a specific type | the orchestrator (many calls) |
| **Budget default** | 3 (mostly for prioritize) | 10 |
| **Staged** | yes (sandbox by default) | no (visible in baseline workspace) |
| **Use when** | the user names a specific call type | the user wants real research progress |

Examples:
- "assess this question" → `/rumil-dispatch assess <id>`
- "find more considerations for this" → `/rumil-dispatch find-considerations <id>`
- "investigate this more" / "run research on this" → `/rumil-orchestrate <id>`
- "give this 10 more calls of budget" → `/rumil-orchestrate <id> --budget 10`
- "produce a four-section view on this" → `/rumil-orchestrate <id> --orchestrator simple_spine --config view_freeform --budget 5.00`
- "smoke-test the view preset on haiku" → `/rumil-orchestrate <id> --orchestrator simple_spine --config view_freeform --model claude-haiku-4-5-20251001 --budget 1.50`

## When the model should invoke this directly

You should call this skill without explicit `/rumil-orchestrate` when the
user's intent is clearly "do real research on this question":

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
- **Orchestrator**: whatever `settings.prioritizer_variant` is
  (normally `two_phase`). Override with `--orchestrator experimental`
  when the user explicitly asks for it.
- **Workspace**: inherited from session state. Override with `--workspace`.
- **Staged**: no — output is immediately visible to the frontend and
  other readers, the same way `main.py` would leave it. This is
  deliberately different from `/rumil-dispatch`, which runs staged
  (sandbox) by default.
- **Origin tag**: `origin=claude-code`, `skill=rumil-orchestrate`, captured
  in `runs.config` so later analyses can filter cc-initiated runs from
  `main.py` runs.

## Arguments

- **`<question_id>`** (positional, required): full UUID or short 8-char ID.
  Must be an existing question in the active workspace.
- **`--budget N`**: per-orchestrator budget. For `two_phase` /
  `experimental`: integer research-call count (default 10). For
  `simple_spine` / `axon`: USD cost cap (default $5.00) — counts input
  + output + cache_create + cache_read at per-model rates from
  `pricing.json`, so cache-write spend (which dominates real cost on
  multi-spawn / multi-delegate runs) is properly bounded.
- **`--orchestrator <variant>`**: `two_phase`, `experimental`,
  `simple_spine`, or `axon`. Defaults to whatever
  `settings.prioritizer_variant` is (normally `two_phase`). Pass
  explicitly whenever the user cares which loop is running. (axon is
  not yet wired into the skill driver — see "Which orchestrator" above.)
- **`--global-prio` / `--no-global-prio`**: force the cross-cutting
  `GlobalPrioOrchestrator` on or off for this invocation. When on, it
  runs *concurrently* with the variant (budget-split). Overrides the
  `ENABLE_GLOBAL_PRIO` env var / `.env` default. Tri-state: omit to
  inherit the env default, pass `--global-prio` to force on, pass
  `--no-global-prio` to force off. Orthogonal to `--orchestrator` (the
  variant still runs as the local prioritiser). Two-phase / experimental
  only.
- **`--smoke-test`**: use Haiku and cap rounds — for fast, cheap testing.
- **`--workspace <name>`**: override the session's active workspace.
- **`--name <text>`**: optional run name; defaults to the question headline.

### SimpleSpine-only flags

(Axon will share the same `--config` / `--model` shape once wired in;
for now it has no driver flags here.)

- **`--config <preset>`** (required when `--orchestrator simple_spine`):
  preset name to load from `src/rumil/orchestrators/simple_spine/configs/`.
- **`--model <id>`**: override every model reference in the config —
  main_model, each subroutine's model, and nested-orch presets'
  main_model — with this value. End-to-end single-model run for cheap
  smoke tests (e.g. `claude-haiku-4-5-20251001`). Auto-disables
  server-side compaction when the override targets a model that
  doesn't support `compact_20260112` (currently any `claude-haiku-*`).
- **`--no-compaction`**: explicitly force-disable server-side compaction
  on the top-level config, regardless of model. Useful when debugging
  compaction-specific issues. Does not propagate to nested orchs unless
  combined with a `--model` override that triggers the auto-disable.

## Invocation

```!
setopt no_glob 2>/dev/null; set -f; PYTHONPATH=.claude/lib uv run python -m rumil_skills.run_orchestrator $ARGUMENTS
```

## After it runs

The orchestrator can run for many minutes. The script streams:
- workspace, question headline, and which orchestrator variant is running
- trace URL — **surface this immediately** so the user can watch progress
  in the browser alongside the CC session
- `→ running <variant> orchestrator (budget N)` confirmation line
- `✓ done: ...` completion line. Two_phase / experimental report
  `budget=used/total` (rumil calls); simple_spine reports
  `cost=$N.NN  spawns=K  reason=...`; axon (when run directly)
  reports `cost=$N.NN  rounds=K  last_status=...` from `OrchResult`.

### Natural next steps to offer

- **See what changed:** `/rumil-show <id>` — refreshed subtree view
- **Read the research:** `/rumil-review <id>` — structured punch list
- **Debug something that looked off:** `/rumil-find-confusion` — scan
  recent calls for model confusion; follow up with `/rumil-trace` on
  the top candidate, then edit the relevant `prompts/*.md` directly
  if a prompt is the root cause
