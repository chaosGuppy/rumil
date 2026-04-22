---
name: rumil-versus-judge
description: Run pairwise judgments on versus essay-continuation pairs using rumil-adjacent judge backends — direct Anthropic (text), rumil's SDK agent with workspace tools (ws), or a full orchestrator run per pair (orch). Use when the user wants to measure how well rumil discriminates on pairs with known ground truth (human continuation vs. model continuations), compare Anthropic judges against OpenRouter judges in versus, evaluate whether workspace material improves judgment, or top up pending judgments after adding new dimensions or models.
allowed-tools: Bash, Read
argument-hint: "--variant text|ws|orch [--workspace <name>] [--dimension <name>...] [--versus-criterion <name>...] [--model <id>...] [--budget N] [--limit N] [--dry-run]"
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
  — defaults to `claude-opus-4-7`). The model is derived, not
  user-configurable per call; override by running rumil in test or
  smoke mode if you need haiku.
- Dimensions default to `general_quality`. Each dimension needs a
  prompt at `prompts/versus-<name>.md`. Currently available:
  `general_quality`, `grounding`. Adding more = drop a new prompt file
  following the existing adapted-for-essays shape — no code changes
  needed.
- `--versus-criterion` accepts any key from
  `versus/src/versus/judge.py:CRITERION_PROMPTS` (e.g.
  `standalone_quality`, `informativeness`, `substance_and_bite`).

## Workspace requirement (ws / orch)

The `--workspace <name>` argument maps to a rumil Project — typically
the user names an existing one that has material relevant to the
essays' topics (e.g. `redwood` for AI-epistemics-related essays).
Versus Question pages created during judging land in that project;
they're tagged `extra.source = "versus"` for later filtering but do
show up in that project's question list.

Supabase must be running locally (`supabase start` in the rumil repo)
for both `ws` and `orch` variants.

## Invocation

```!
cd /Users/brian/code/rumil && uv run --with httpx --with pydantic --with pyyaml versus/scripts/run_rumil_judgments.py $ARGUMENTS
```

Versus has its own `pyproject.toml` and isn't installed in rumil's
`.venv`, so we pass its runtime deps explicitly to `uv run --with`.

Typical invocations:

- `--variant text --dry-run` — list pending Anthropic-text judgments
- `--variant ws --workspace redwood --dry-run` — list pending ws judgments
- `--variant ws --workspace redwood --limit 5` — run 5 ws judgments
- `--variant ws --workspace redwood --dimension grounding --versus-criterion standalone_quality --limit 5` — mix tasks
- `--variant orch --workspace redwood --budget 1 --limit 3` — 3 orch judgments at minimum budget

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

Rough per-judgment estimates:

| Variant | Model | $/judgment (rough) | 100 judgments |
|---|---|---|---|
| text | claude-sonnet-4-6 | $0.03-0.08 | $3-8 |
| text | claude-opus-4-7 | $0.20-0.40 | $20-40 |
| ws | claude-opus-4-7 | $0.50-2.00 (multi-turn + tool use) | $50-200 |
| orch | claude-opus-4-7, budget=1 | $1.00-5.00 (research + closer) | $100-500 |

The `ws` and `orch` cost ranges are especially wide because they
depend on how much the agent/orchestrator uses workspace tools. For
first runs, always start with `--limit 3` or `--limit 5` to get actual
numbers before scaling.

## Other caveats

- **`ws` puts Question pages in the chosen workspace**. Tagged
  `extra.source = "versus"` for filterability, but they will appear in
  the project's question list. Accept or clean up after.
- **`orch` creates a rumil Run per pair.** Each shows up on
  `/traces`. Use a low `--limit` initially.
- **Re-running is free.** Dedup keys include the variant, model,
  workspace, dimension (or versus criterion), and budget, so any
  combination change produces fresh rows without clobbering existing
  ones.
- **Rumil trace UI requires rumil's frontend** (`./scripts/dev-api.sh`
  + `cd frontend && pnpm dev`). Trace URLs emitted by the script point
  at `settings.frontend_url` (default http://127.0.0.1:3000).
