---
name: rumil-versus-judge
description: Run pairwise judgments on versus essay-continuation pairs. Default mode is the unified blind path (single-turn LLM call, no tools, no DB) — claude-* models route direct to Anthropic, others through OpenRouter. --variant ws adds a rumil SDK agent with workspace-exploration tools; --variant orch fires a full TwoPhaseOrchestrator run per pair. Use when the user wants to measure how rumil discriminates on pairs with known ground truth (human continuation vs. model continuations), compare blind judges against workspace-aware ones, or top up pending judgments after adding new dimensions or models.
allowed-tools: Bash, Read
argument-hint: "[--variant ws|orch] [--workspace <name>] [--model opus|sonnet|haiku|<full-id> ...] [--dimension <name>...] [--essay <id>...] [--include-stale] [--contestants <csv>] [--vs-human] [--budget N] [--limit N] [--concurrency N] [--current-only] [--persist] [--dry-run]"
---

# rumil-versus-judge

Runs pairwise judgments on versus essay-continuation pairs against a
rumil-adjacent judge backend, writing into the `versus_judgments`
Postgres table. The versus UI picks up the new rows automatically;
rumil paths also store project_id / run_id / rumil_call_id so the
`/inspect` page can link back to the rumil trace.

## Three modes

| Mode | What it does | judge_model format | Needs supabase? |
|---|---|---|---|
| Blind (default, no `--variant`) | Single-turn LLM call, no tools, no DB. Each `--model` routed: `claude-*` direct to Anthropic, others via OpenRouter. Same shell/dimension prompt across providers. | `blind:<model>:<dim>:c<hash8>` | no |
| `ws` | One VERSUS_JUDGE rumil agent call per pair with single-arm workspace-exploration tools (search / load_page / explore_subgraph). Uses the essay-adapted rumil dimension prompt selected via `--dimension`. | `rumil:ws:<model>:<dim>:c<hash8>` | **yes** |
| `orch` | Per-pair: create Question page, fire `TwoPhaseOrchestrator` at configurable budget, then a closing call extracts the 7-point label. Produces a full research trace per pair. | `rumil:orch:<model>:<dim>:c<hash8>` | **yes** |

`<dim>` is the rumil dimension name selected via `--dimension`
(e.g. `general_quality`, `grounding`). The trailing `c<hash8>` is the
first eight hex chars of the row's `judge_inputs_hash` — the dedup
primitive. The blind shell is **without** tool advertisements (no
scope-question references, no `load_page`/`search_workspace`
mentions); ws/orch shell keeps them. Both modes share the same
template — see `rumil/versus_prompts.py` for the substitution dicts.

Each row carries a structured `judge_inputs: dict`; the DB generates
`judge_inputs_hash` from its canonical-form JSON. The blob is built by
`versus.judge_config.make_judge_config` and covers every input the
judge saw — model, dimension, the per-model `model_config` snapshot
(sampling, thinking, effort, max_thinking_tokens, service_tier — full
`ModelConfig.to_record_dict()` from the registry), prompt content,
tool descriptions, pair surface, code fingerprint, workspace state,
budget, closer config — plus `text_a_id` / `text_b_id` and `order`
added at write time. Editing the registry, swapping a prompt, or
touching any code the fingerprint covers auto-forks the hash; no
manual version bump to remember. See `versus/AGENT.md` for the full
schema and the registry shape.

## When to use

| Intent | Skill / command |
|---|---|
| "run blind judges on versus pairs (any model — claude or otherwise)" | this skill (default mode) |
| "run rumil ws/orch judges on versus pairs" | this skill (`--variant ws|orch`) |
| "A/B eval two rumil research runs" | rumil's `main.py --ab-eval A B` — different system |

Versus must already have completions in the `versus_texts` table.
If it doesn't, the user needs `versus/scripts/fetch_essays.py` +
`run_completions.py` first — flag that and stop. Local Supabase must
also be running (`supabase start` from the rumil repo root).

**Before any topup run, check for staleness:**

```bash
# run from the rumil repo root (not versus/) so rumil is importable
uv run python versus/scripts/status.py
```

Compares cached completion / paraphrase / judgment rows against the
current essay cache. A row is stale if its `essay_id` isn't in the
current cache (essay removed, renamed, or at an older
`schema_version` — same gate as the API's `_build_essays_status`) or
its `prefix_config_hash` doesn't match the current hash (essay content
or prefix params changed). Paraphrases also bucket by sampling hash to
catch prompt version bumps.

Exit code 2 + a `STALE` banner means a topup will silently keep
extending rows against OLD essay text / outdated prompts unless the
user first re-runs `run_paraphrases.py` + `run_completions.py` to
write fresh rows under the new keys. Surface the warning to the user;
don't proceed on stale data without confirmation.

The same three staleness buckets (`current` / `stale_prefix` or
`stale_prompt` / `unknown_essay`) are what `/versus/results` filters
on in the UI, so `status.py`'s numbers should match what you see there.

## Env & config

- `ANTHROPIC_API_KEY` and `OPENROUTER_API_KEY` resolve from `versus/.env`,
  then `<rumil-root>/.env`, then the process environment. Files override
  env so per-project `.env` takes precedence. The blind path may need
  either or both depending on the models passed; ws/orch only need
  `ANTHROPIC_API_KEY` (claude models only).
- All modes take their model via `--model`. Accepts a short alias
  (`opus` / `sonnet` / `haiku`), a bare Anthropic id (`claude-*`), or
  an OpenRouter id (`provider/model`).
- Blind (default): repeat `--model` for multi-model runs. Each model
  routes by id — `claude-*` direct to Anthropic, anything else through
  OpenRouter. Defaults to `cfg.judging.models` in `versus/config.yaml`.
- `ws` / `orch` variants: pass at most one `--model` (default: `opus`).
  Passed explicitly through the bridge via
  `override_settings(rumil_model_override=...)` — no env-var
  ordering. Picking the model matters: opus and sonnet produce
  meaningfully different verdicts on the same pairs, so treat it as
  part of the judge identity.
- New aliases land via `RUMIL_MODEL_ALIASES` in `src/rumil/settings.py`;
  the script imports the dict from there.
- Dimensions default to `general_quality`. Each dimension needs a
  prompt at `prompts/versus-<name>.md`. Currently available:
  `general_quality`, `grounding`. Adding more = drop a new prompt file
  following the existing adapted-for-essays shape — no code changes
  needed.
- `--current-only` — skip groups whose `prefix_config_hash` isn't the
  current one for the essay. Protects against judging stale rows after
  an essay re-import; pair with `scripts/status.py` to detect staleness.
- `--persist` — ws/orch only. Disables the default staged mode and
  writes versus-created pages to the baseline workspace.
- `--concurrency N` — concurrent LLM calls. Defaults: blind =
  `cfg.per_model_concurrency` per model (usually 8), `ws` = 2,
  `orch` = 1 (serial). Raise `orch` concurrency cautiously (each pair
  fires a full TwoPhaseOrchestrator with lots of DB traffic); 2 is
  usually fine and roughly halves wall time.

## Targeted pair selection (ws / orch)

The default planner enumerates every pending pair × task in every
essay. For focused comparisons (especially expensive paths like orch)
use these filters — all shared across ws and orch:

- `--essay <id>` (repeatable) — restrict to specific essays.
- `--include-stale` — opt out of the active-set default. By default
  the planner restricts to the canonical active essay set (current
  `schema_version`, not in `cfg.essays.exclude_ids` — same gate
  `/versus` applies). Pass this to widen to every essay with rows in
  `versus_texts`, including off-feed or older-schema rows. Composes
  with `--essay` (intersected when not stale).
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

**Prod has a dedicated `versus` workspace** (intentionally empty) for
ws/orch runs from versus. Pass `--workspace versus --prod` to use it.
Reusing one shared workspace keeps the staged subtrees from each run
discoverable in one place. New workspaces must be created explicitly
via rumil's main.py before a `--prod` ws/orch run — the resolver
fails-loud on missing names (typo protection).

By default `ws` / `orch` runs are **staged** (`staged=True` on
rumil's DB) on both local and prod. The agent still reads baseline
workspace material normally, but any pages versus creates during the
run (the per-pair Question, plus the orchestrator's research subtree
for `orch`) are scoped to the run's staged view — invisible to other
readers of the workspace. Pass `--persist` to disable staging and
write pages to the baseline. Pages are also tagged
`extra.source = "versus"` in both modes, so filtering after the fact
is possible.

For local runs, Supabase must be running (`supabase start` in the
rumil repo). For `--prod` runs, no local Supabase needed — the script
writes to prod versus_db AND prod rumil DB. Note: with prod runs, the
trace URL printed (`/traces/<run_id>`) points at the local frontend's
configured `frontend_url` — you may need to point your local frontend
at the prod API or visit `rumil.ink/traces/<run_id>` directly to see
the trace.

## Invocation

```!
cd /Users/brian/code/rumil && uv run --with httpx --with pydantic --with pyyaml versus/scripts/run_rumil_judgments.py $ARGUMENTS
```

Versus has its own `pyproject.toml` and isn't installed in rumil's
`.venv`, so we pass its runtime deps explicitly to `uv run --with`.

Typical invocations (substitute the user's chosen workspace for `<WS>`):

- `--dry-run` — list pending blind judgments (uses `cfg.judging.models`)
- `--model sonnet --dry-run` — pending blind judgments restricted to sonnet
- `--variant ws --workspace <WS> --dry-run` — list pending ws judgments
- `--variant ws --workspace <WS> --limit 5` — run 5 ws judgments
- `--variant ws --workspace <WS> --model sonnet --limit 5` — run on sonnet instead of opus
- `--variant ws --workspace <WS> --dimension general_quality --dimension grounding --limit 5` — run multiple dimensions
- `--variant orch --workspace <WS> --budget 4 --limit 3` — 3 orch judgments at minimum budget (TwoPhaseOrchestrator rejects budget < 4)

## What to surface

All modes print:
- `[plan] N ... judgments ...` — pending count. Use this before confirming cost.
- `[done i/N] <dedup_key>  label=... trace=<url>` (ws/orch) or `[done ...]` (blind)
- `[run] <trace_url>` (ws session-level; orch per-pair)
- `[err ] <key>: <msg>` on failure (run continues for other pairs)

Surface any printed trace URLs to the user immediately. For `ws` and
`orch` runs this is a hard requirement, not a nice-to-have — the user
wants to follow along live. Specifically:

- If foregrounded, report `[run] <url>` lines as they stream.
- If backgrounded (logfile pattern below), poll the logfile for `[run]`
  lines (`grep '^\[run\]' /tmp/versus-run-<id>.log`) and post the URL
  to the user as soon as one appears — do not wait for the run to
  finish. For `orch` there's one `[run]` URL per pair; post each new
  one as it shows up.

After a run completes, suggest the versus UI in the rumil frontend
(`http://localhost:300X/versus/inspect?essay=<id>`, served by the
running rumil dev server) — the `/inspect` page shows rumil judgments
per essay with trace links that navigate in-app to `/traces/[runId]`.

## Cost confirmation

**Always `--dry-run` first** and **confirm with the user before firing
if expected cost is > ~$10**.

Per-judgment estimates. **Bold** values are measured in practice on the
versus forethought-essay pairs against the `redwood` workspace; others
are extrapolations.

| Mode | Model | $/judgment | 100 judgments |
|---|---|---|---|
| blind | sonnet (4-6) | $0.03-0.10 | $3-10 |
| blind | opus (4-7) | $0.20-0.45 | $20-45 |
| blind | gemini / gpt-5.4 (via OpenRouter) | ~$0.01-0.05 depending on model | $1-5 |
| ws | haiku (4-5) | $0.05-0.25 | $5-25 |
| ws | sonnet (4-6) | **~$0.12** (observed; range $0.09-0.14) | ~$12 |
| ws | opus (4-7) | **~$0.53** (observed; range $0.35-0.73) | ~$53 |
| orch | sonnet, budget=4 | ~$1-3 (varies with tool use + subtree depth) | $100-300 |
| orch | opus, budget=4 | ~$3-10 | $300-1000 |

Practical guidance: pair `--model sonnet` with the `ws` variant
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
when the work itself completes (rows still land in versus_judgments
because Python holds its own DB connection).

Safe pattern for long backgrounded runs:

```
uv run ... versus/scripts/run_rumil_judgments.py <flags> > /tmp/versus-run-<id>.log 2>&1
```

with `run_in_background: true`. Watch progress by querying
`versus_judgments` (counts increment regardless of stdout) and the
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
  closely; opus is more human-biased. Treat `--model` as part
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
  workspace, dimension (or versus criterion), budget, the full
  per-model `model_config` snapshot from the registry (via
  `:m<hash>`), tool-prompt contents (via `:t<hash>` for ws/orch),
  and `order` slot — so any combination change produces fresh rows
  without clobbering existing ones. Editing `versus/config.yaml`
  `models:` for the judge model forks the hash naturally; old rows
  stay valid as the prior config. `order` is currently always
  single-order; the slot exists so mirror-mode can be switched on
  without a migration.
- **Rumil trace UI requires rumil's frontend** (`./scripts/dev-api.sh`
  + `cd frontend && pnpm dev`). Trace URLs emitted by the script point
  at `settings.frontend_url` (default http://127.0.0.1:3000).

## What forks `config_hash`

`config_hash` is what `judgment_key` keys on. Anything in the
structured config dict produced by
`versus.judge_config.make_judge_config` flows into it. So forking is
automatic for:

- Edits to `prompts/versus-judge-shell.md` or
  `prompts/versus-<dim>.md` (covered via the rendered prompt's sha).
- Sampling-dict changes (temperature / max_tokens).
- Tool-description / docstring edits on `search_workspace` /
  `load_page` / `explore_subgraph` (ws/orch only).
- Edits to the agent-visible Question page surface
  (`_format_pair_content`, `_build_headline`, `_versus_extra` key
  schema) — `pair_surface_hash` covers them.
- Edits to bridge / orchestrator / call code under the
  `JUDGE_CODE_FINGERPRINT_DIRS` and `JUDGE_CODE_FINGERPRINT_FILES`
  paths — `code_fingerprint` covers them per file/directory.
- Mutations to baseline workspace pages or links between runs —
  `workspace_state_hash` is a cheap watermark that bumps on any
  change visible to the agent's tools.
- Orch closer config (max_turns, disallowed_tools, render knobs) —
  `closer_hash` covers it.

If a surface isn't on this list and you suspect it should fork the
key, the right fix is to extend `make_judge_config` (and the matching
axis in `project_config_to_axes`), not to add a willpower knob.
