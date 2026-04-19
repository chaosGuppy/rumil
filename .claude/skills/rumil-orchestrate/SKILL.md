---
name: rumil-orchestrate
description: Fire the rumil orchestrator against an existing question — a full multi-call research loop with a budget. This is the CC-initiated equivalent of `main.py --continue <qid> --budget N`. Use when the user wants real research done on a question, not just a single call. For one targeted call, use /rumil-dispatch instead. Budget defaults to 10; since that's not cheap, confirm with the user before firing if they didn't specify. Trigger when the user says things like "investigate this more", "run some research on this", "give Q# N calls of budget", or right after /rumil-ask when they want to immediately start investigating.
allowed-tools: Bash
argument-hint: "<question_id> [--budget N] [--orchestrator two_phase|experimental|worldview|distill_first|critique_first|cascade|source_first] [--global-prio|--no-global-prio] [--smoke-test]"
---

> **Under the hood:** this skill, the CLI `main.py --continue`, the
> `/api/questions/{id}/continue` endpoint, and the chat `orchestrate` tool
> all route through the same `rumil.dispatch.dispatch_orchestrator` function,
> which reads variants from the `rumil.orchestrators.registry.ORCHESTRATORS`
> registry. Use `GET /api/capabilities` to see every variant available on a
> given server.

# rumil-orchestrate

Fires the rumil orchestrator against an existing question with a budget.
This is the CC-initiated equivalent of `main.py --continue <id> --budget N`.
The orchestrator dispatches a *sequence* of calls (prioritize, scout,
find-considerations, assess, etc.) based on the chosen orchestrator
variant and what the workspace needs, until the budget is consumed or
the orchestrator decides the question is done.

## Which orchestrator

Rumil ships several top-level research-loop orchestrators. The full set
lives in `src/rumil/orchestrators/registry.py` and is exposed via
`GET /api/capabilities` — the canonical list. Quick summary as of writing:

- **`two_phase`** (default, stable) — balanced breadth + deepening.
- **`experimental`** — in-flight variants; currently mirrors two_phase.
- **`worldview`** — cycles explore/evaluate modes; drains CASCADE_REVIEW.
- **`distill_first`** — view-centric; summarizes before broad discovery.
- **`critique_first`** — how-true/how-false scouts before find-considerations.
- **`cascade`** — drains pending cascade assessments; low-cost follow-up.
- **`source_first`** — web research / ingest before find-considerations.

`refine_artifact` is registered but CLI-only (different run shape; not
available via chat or this skill). `ClaimInvestigationOrchestrator` and
`RobustifyOrchestrator` are sub-orchestrators, not research loops.

The chosen variant is captured in `runs.config.prioritizer_variant` so
later analyses can filter by orchestrator.

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
- **`--budget N`**: research-call budget. Default 10.
- **`--orchestrator <variant>`**: `two_phase` or `experimental`. Defaults
  to whatever `settings.prioritizer_variant` is (normally `two_phase`).
  Pass explicitly whenever the user cares which loop is running.
- **`--global-prio` / `--no-global-prio`**: force the cross-cutting
  `GlobalPrioOrchestrator` on or off for this invocation. When on, it
  runs *concurrently* with the variant (budget-split). Overrides the
  `ENABLE_GLOBAL_PRIO` env var / `.env` default. Tri-state: omit to
  inherit the env default, pass `--global-prio` to force on, pass
  `--no-global-prio` to force off. Orthogonal to `--orchestrator` (the
  variant still runs as the local prioritiser).
- **`--smoke-test`**: use Haiku and cap rounds — for fast, cheap testing.
- **`--workspace <name>`**: override the session's active workspace.
- **`--name <text>`**: optional run name; defaults to the question headline.

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
- `✓ done: budget=used/total` completion line

### Natural next steps to offer

- **See what changed:** `/rumil-show <id>` — refreshed subtree view
- **Read the research:** `/rumil-review <id>` — structured punch list
- **Debug something that looked off:** `/rumil-find-confusion` — scan
  recent calls for model confusion; follow up with `/rumil-trace` on
  the top candidate, then edit the relevant `prompts/*.md` directly
  if a prompt is the root cause
