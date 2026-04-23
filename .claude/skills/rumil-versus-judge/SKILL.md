---
name: rumil-versus-judge
description: Run pairwise judgments on versus essay-continuation pairs using rumil-adjacent judge backends — direct Anthropic (text), rumil's SDK agent with workspace tools (ws), or a full orchestrator run per pair (orch). Use when the user wants to measure how well rumil discriminates on pairs with known ground truth (human continuation vs. model continuations), compare Anthropic judges against OpenRouter judges in versus, evaluate whether workspace material improves judgment, or top up pending judgments after adding new dimensions or models.
allowed-tools: Bash, Read
argument-hint: "--variant text|ws|orch [--workspace <name>] [--rumil-model opus|sonnet|haiku] [--dimension <name>...] [--versus-criterion <name>...] [--essay <id>...] [--contestants <csv>] [--vs-human] [--model <id>...] [--budget N] [--limit N] [--dry-run]"
---

# rumil-versus-judge

Runs pairwise judgments on versus essay-continuation pairs against a
rumil-adjacent judge backend, writing into the same
`versus/data/judgments.jsonl` that OpenRouter judges use. The versus UI
picks up the new rows automatically; rumil paths also mirror a trace
URL + 7-point preference label + call/run/question IDs so the `/inspect`
page can link back to the rumil trace.

## Three variants

| Variant | What it does | judge_model format | Needs supabase? |
|---|---|---|---|
| `text` (default) | Single-turn Anthropic call, versus judge prompt. Same shape as OpenRouter judges, different model axis. | `anthropic:<model>` | no |
| `ws` | One VERSUS_JUDGE rumil agent call per pair with single-arm workspace-exploration tools (search / load_page / explore_subgraph). Dimension prompt defaults to essay-adapted rumil dimensions; `--versus-criterion` also available for direct comparison. | `rumil:ws:<model>:<ws_short>:<task>` | **yes** |
| `orch` | Per-pair: create Question page, fire `TwoPhaseOrchestrator` at configurable budget, then a closing call extracts the 7-point label. Produces a full research trace per pair. | `rumil:orch:<model>:<ws_short>:b<N>:<task>` | **yes** |

`<task>` is either a rumil dimension name (e.g. `general_quality`) or
`versus_<criterion>` (e.g. `versus_standalone_quality`) when
`--versus-criterion` is set.

## When to use

| Intent | Skill / command |
|---|---|
| "run rumil judges on versus pairs" (any variant) | this skill |
| "run OpenRouter judges (Gemini/GPT)" | `versus/scripts/run_judgments.py` — not this skill |
| "A/B eval two rumil research runs" | rumil's `main.py --ab-eval A B` — different system |

Versus must already have completions cached
(`versus/data/completions.jsonl`). If it doesn't, the user needs
`versus/scripts/fetch_essays.py` + `run_paraphrases.py` +
`run_completions.py` first — flag that and stop.

## Env & config

- `ANTHROPIC_API_KEY` resolves from `versus/.env`, then
  `<rumil-root>/.env`, then the process environment. Files override env
  so per-project `.env` takes precedence.
- Anthropic models for the `text` variant come from
  `versus/config.yaml` under `judging.anthropic_models`, overridable
  via `--model`.
- `ws` / `orch` variants use rumil's configured model (`settings.model`
  — defaults to `claude-opus-4-7`). Override per-run with
  `--rumil-model opus|sonnet|haiku`; the flag sets
  `RUMIL_MODEL_OVERRIDE` before rumil imports so `run_sdk_agent` and
  `text_call` see it. Picking the model matters — opus and sonnet
  produce meaningfully different verdicts on the same pairs, so treat
  the model as part of the judge identity.
- Dimensions default to `general_quality`. Each dimension needs a
  prompt at `prompts/versus-<name>.md`. Currently available:
  `general_quality`, `grounding`. Adding more = drop a new prompt file
  following the existing adapted-for-essays shape — no code changes
  needed.
- `--versus-criterion` accepts any key from
  `versus/src/versus/judge.py:CRITERION_PROMPTS` (e.g.
  `standalone_quality`, `informativeness`, `substance_and_bite`).

## Targeted pair selection (ws / orch)

The default planner enumerates every pending pair × task in every
essay. For focused comparisons (especially expensive paths like orch)
use these filters — all shared across ws and orch:

- `--essay <id>` (repeatable) — restrict to specific essays.
- `--contestants <csv>` — only pairs where BOTH source_ids are in the
  list. Controls which contestants get compared against each other.
- `--vs-human` — only pairs where one side is `human`.

A useful pattern for benchmarking against "did rumil pick the human
continuation?": pick one essay, all three versus completion models, and
force-vs-human:

```
--essay <essay_id> --vs-human --contestants human,google/gemini-3-flash-preview,openai/gpt-5.4,openai/gpt-5.4-mini
```

This plans exactly 3 pairs: (flash, human), (gpt-5.4, human),
(mini, human) — covers the quality spectrum on one essay. Repeat
`--essay` to span multiple essays on the same pattern.

## Workspace requirement (ws / orch)

The `--workspace <name>` argument maps to a rumil Project — no
default; the user must name one. For the `ws` / `orch` variants to do
better than text-only judgment, that workspace should have material
relevant to the essays' topics.

By default `ws` / `orch` runs are **staged** (`staged=True` on
rumil's DB). The agent still reads baseline workspace material
normally, but any pages versus creates during the run (the per-pair
Question, plus the orchestrator's research subtree for `orch`) are
scoped to the run's staged view — invisible to other readers of the
workspace. Pass `--persist` to disable staging and write pages to the
baseline. Pages are also tagged `extra.source = "versus"` in both
modes, so filtering after the fact is possible.

Supabase must be running locally (`supabase start` in the rumil repo)
for both `ws` and `orch` variants.

## Invocation

```!
cd /Users/brian/code/rumil && uv run --with httpx --with pydantic --with pyyaml versus/scripts/run_rumil_judgments.py $ARGUMENTS
```

Versus has its own `pyproject.toml` and isn't installed in rumil's
`.venv`, so we pass its runtime deps explicitly to `uv run --with`.

Typical invocations (substitute the user's chosen workspace for `<WS>`):

- `--variant text --dry-run` — list pending Anthropic-text judgments
- `--variant ws --workspace <WS> --dry-run` — list pending ws judgments
- `--variant ws --workspace <WS> --limit 5` — run 5 ws judgments
- `--variant ws --workspace <WS> --rumil-model sonnet --limit 5` — run on sonnet instead of opus
- `--variant ws --workspace <WS> --dimension grounding --versus-criterion standalone_quality --limit 5` — mix tasks
- `--variant orch --workspace <WS> --budget 4 --limit 3` — 3 orch judgments at minimum budget (TwoPhaseOrchestrator rejects budget < 4)

## What to surface

All variants print:
- `[plan] N ... judgments ...` — pending count. Use this before confirming cost.
- `[done i/N] <dedup_key>  label=... trace=<url>` (ws/orch) or `[done ...]` (text)
- `[run] <trace_url>` (ws session-level; orch per-pair)
- `[err ] <key>: <msg>` on failure (run continues for other pairs)

Surface any printed trace URLs to the user immediately. After a run
completes, suggest the versus UI (`cd versus && uv run
scripts/serve_ui.py` → http://127.0.0.1:8765/inspect?essay=<id>) —
the `/inspect` page now shows rumil judgments per essay with trace
links inline.

## Cost confirmation

**Always `--dry-run` first** and **confirm with the user before firing
if expected cost is > ~$10**.

Per-judgment estimates. **Bold** values are measured in practice on the
versus forethought-essay pairs against the `redwood` workspace; others
are extrapolations.

| Variant | Model | $/judgment | 100 judgments |
|---|---|---|---|
| text | sonnet (4-6) | $0.03-0.08 | $3-8 |
| text | opus (4-7) | $0.20-0.40 | $20-40 |
| ws | haiku (4-5) | $0.05-0.25 | $5-25 |
| ws | sonnet (4-6) | **~$0.12** (observed; range $0.09-0.14) | ~$12 |
| ws | opus (4-7) | **~$0.53** (observed; range $0.35-0.73) | ~$53 |
| orch | sonnet, budget=4 | ~$1-3 (varies with tool use + subtree depth) | $100-300 |
| orch | opus, budget=4 | ~$3-10 | $300-1000 |

Practical guidance: pair `--rumil-model sonnet` with the `ws` variant
for a much cheaper first pass that's still a real test of
workspace-access discrimination (opus judges differently — see the
methodology note below). For expensive paths, always start with
`--limit 3` or `--limit 5` and confirm actual per-judgment cost from
the first results before scaling. Use `--essay` + `--vs-human` +
`--contestants` to keep scope deliberate instead of fanning out.

## Running long batches in the background

Large ws/orch runs can take 10+ minutes. If you want to background the
run, fire it with `run_in_background: true` from the start on a single
non-compound command; do NOT fire a compound `scrub && uv run ...` and
manually background it mid-flight — when Claude Code's foreground
process group detaches, the `&&` chain breaks between cmd1 and cmd2
and cmd2 silently never fires. Also: manual-background tears down the
tool's stdout capture, so `[plan] / [done]` lines get dropped even
when the work itself completes (rows still land in judgments.jsonl
because Python holds its own fd).

Safe pattern for long backgrounded runs:

```
uv run ... versus/scripts/run_rumil_judgments.py <flags> > /tmp/versus-run-<id>.log 2>&1
```

with `run_in_background: true`. Watch progress by tailing
`judgments.jsonl` (rows increment regardless of stdout) and the
explicit logfile.

## Methodology pitfalls (observed on forethought essays vs. `redwood`)

These are worth mentioning to users interpreting results so they don't
over-read any single number:

- **The "ground truth = human wins" framing is weaker than expected.**
  On the forethought essays, all non-opus judges (OpenRouter gemini /
  gpt-5.4 / gpt-5.4-mini AND rumil sonnet ws) reliably pick the
  gpt-5.4 continuation over the human author's on multiple essays.
  Rumil opus ws is the outlier that stays close to always picking
  human. If "did the judge pick human?" is the metric, opus always
  looks best — but that might be measuring opus's human-bias, not
  discrimination.
- **Opus and sonnet produce meaningfully different verdicts on the
  same pairs through ws.** Sonnet tracks OpenRouter consensus more
  closely; opus is more human-biased. Treat `--rumil-model` as part
  of the judge identity, not an implementation detail.
- **Rumil systematically disagrees with OpenRouter on the mini vs
  human pairs** (rumil prefers human, OpenRouter mostly picks mini)
  — consistent across sonnet and opus. This is a real signal worth
  investigating, not noise.

## Other caveats

- **`ws` / `orch` stage their workspace additions by default.** Versus
  Question pages (and, for `orch`, the orchestrator's research
  subtree) are scoped to the staged run and invisible to baseline
  readers of the workspace. Pass `--persist` to write them to the
  baseline instead. Either way they're tagged `extra.source = "versus"`.
- **`orch` creates a rumil Run per pair.** Each shows up on
  `/traces`. Use a low `--limit` initially. Minimum budget is 4
  (`TwoPhaseOrchestrator` rejects anything smaller with a clear
  error).
- **Re-running is free.** Dedup keys include the variant, model,
  workspace, dimension (or versus criterion), and budget, so any
  combination change produces fresh rows without clobbering existing
  ones.
- **Rumil trace UI requires rumil's frontend** (`./scripts/dev-api.sh`
  + `cd frontend && pnpm dev`). Trace URLs emitted by the script point
  at `settings.frontend_url` (default http://127.0.0.1:3000).
