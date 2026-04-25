---
name: rumil-versus-openrouter
description: Run the OpenRouter-backed versus scripts — completions, paraphrases, and pairwise judgments — via versus/scripts/run_{completions,paraphrases,judgments}.py. Handles the uv --with dance, the --config / path anchoring, and the --active canonical-set flag. Use when the user wants to produce or top up versus jsonl rows with OpenRouter models (GPT / Gemini / etc.), NOT rumil-adjacent judges (see rumil-versus-judge for those).
allowed-tools: Bash, Read
argument-hint: "--op completions|paraphrases|judgments [--model <id>...] [--judge-model <id>...] [--essay <id>...] [--active] [--vs-human] [--contestants <csv>] [--criterion <name>...] [--limit N] [--current-only] [--dry-run]"
---

# rumil-versus-openrouter

Wraps the three OpenRouter-backed versus scripts that write into
`versus/data/*.jsonl`:

| `--op` | Script | What it writes |
|---|---|---|
| `completions` | `run_completions.py` | `completions.jsonl` — model continuations + a human baseline row per essay |
| `paraphrases` | `run_paraphrases.py` | `paraphrases.jsonl` — same-model-rewrites of each essay's remainder |
| `judgments` | `run_judgments.py` | `judgments.jsonl` — blind pairwise verdicts over contestants in `completions.jsonl` |

All three hit OpenRouter. For Anthropic-direct / rumil:ws / rumil:orch
judges, use **`rumil-versus-judge`** instead — different backend, same
output file.

## When to use

| Intent | This skill? |
|---|---|
| "run a flash / gpt-5.4 / gemini completion on essay X" | yes |
| "top up OpenRouter judgments after adding a model" | yes |
| "regenerate paraphrases after a prompt bump" | yes |
| "run a rumil:ws / rumil:orch judge" | **no** — use `rumil-versus-judge` |
| "run an Anthropic-direct judge" | **no** — `rumil-versus-judge --variant text` |
| "pre-flight check before a topup" | **no** — see `status.py` below |

## Before any run: check staleness

```bash
# run from rumil repo root
uv run python versus/scripts/status.py
```

Reports `current / stale_prefix / stale_prompt / unknown_essay` buckets
for all three jsonl stores. Exit code 2 + `STALE` banner = existing
rows reference OLD essay text or outdated prompts. Topups against stale
rows silently extend that staleness — re-run paraphrases + completions
first if the user isn't explicit. Same gate `/versus` uses in the UI.

## The `--active` flag

All three scripts now support `--active` — restrict to the canonical
eval set: current `schema_version` and not in `cfg.essays.exclude_ids`.
This is the 25-essay set `/versus` enumerates.

Without `--active`, the scripts still honor `exclude_ids` (so excluded
essays never get rows), but they'll happily touch older-schema essays
if those are still in the cache. Use `--active` to mirror what the UI
sees — the default for "run flash across the active set" style tasks.

`--active` composes with `--essay`: both act as filters (AND). So
`--active --essay forethought__broad-timelines` means "the essay, only
if it's also in the active set."

## Invocation

All three scripts resolve paths relative to `versus/` regardless of
cwd. Run from the rumil repo root so rumil is importable (the judge
script imports `rumil.versus_bridge`):

```!
cd /Users/brian/code/rumil && uv run --with httpx --with pydantic --with pyyaml python versus/scripts/run_$OP.py $ARGUMENTS
```

The `--with` flags are versus's runtime deps (it's a separate
pyproject, not installed in rumil's `.venv`). If you forget them the
scripts fail with `ModuleNotFoundError: httpx` or similar.

## Typical invocations

**Flash completion + judgment across the active set** (the 25-essay
canonical set, ~30 API calls at flash rates):

```
--op completions --active --model google/gemini-3-flash-preview
--op judgments --active --judge-model google/gemini-3-flash-preview --vs-human --contestants human,google/gemini-3-flash-preview
```

**Single-essay dry-run** (verify what would fire):

```
--op judgments --essay redwood__my-picture-of-the-present-in-ai --vs-human --contestants human,google/gemini-3-flash-preview --dry-run
```

**Top up a new judge across existing pairs**:

```
--op judgments --active --judge-model openai/gpt-5.4
```

Default configured models/judges/criteria come from
`versus/config.yaml`:
- `completion.models` → flash + gpt-5.4-mini
- `judging.models` → flash + gpt-5.4-mini + gpt-5.4
- `judging.criteria` → `general_quality` only (other versus-side
  criteria like `substance_and_bite` were retired with
  `JUDGE_PROMPT_VERSION=2`)

Override with `--model` / `--judge-model` / `--criterion` (all
repeatable).

## Filter flags (judgments)

Shared with `rumil-versus-judge` so muscle memory carries over:

- `--essay <id>` (repeatable) — restrict to specific essays.
- `--active` — the 25-essay canonical set.
- `--contestants <csv>` — only pairs where BOTH `source_id`s are in
  the list.
- `--vs-human` — only pairs where one side is `human`.
- `--current-only` — skip groups whose `prefix_config_hash` isn't
  current (i.e. reference older essay markdown).
- `--limit N` — cap number of judgments.
- `--dry-run` — print the plan and exit.

The pattern `--vs-human --contestants human,<model>` restricts to
exactly (model, human) pairs — useful for "did the judge pick human?"
style evals.

## What to surface

All three scripts print:
- `[plan] N ... calls (concurrency=K)` — count, use before confirming cost.
- `[done i/N] <dedup_key>` — one line per successful row.
- `[skip] <key>` — dedup hit, nothing written.
- `[skip-refusal] <essay> / <model>` — judgments only; pair skipped because
  a contestant's completion was flagged as a refusal. Expected; not an error.
- `[err ] <key>: <msg>` — failure; other items still run.

## Cost

OpenRouter pricing varies by model; rough per-call costs for this
workflow's default max_tokens settings:

| Model | Completion | Judgment |
|---|---|---|
| google/gemini-3-flash-preview | ~$0.002 | ~$0.005 |
| openai/gpt-5.4-mini | ~$0.01 | ~$0.02 |
| openai/gpt-5.4 | ~$0.05 | ~$0.10 |

The 25-essay active set at flash = ~$0.15 for completions + ~$0.15
per judge for vs-human judgments. Cheap enough to run without
confirming. GPT-5.4 at the same scope is ~$3 for completions + $3
per judge — confirm if > ~$10.

Always `--dry-run` first for anything beyond flash to see the exact
plan count; it's free and takes a second.

## Dedup & re-runs

Re-runs are free. Each row's `key` encodes the essay, prefix hash,
sources, criterion, judge_model (with `:p<hash>:v<N>:s<hash>` tags),
and order slot. Any change in those inputs produces a fresh row;
identical inputs `[skip]`. So adding a model or criterion to the
config and re-running fills gaps without clobbering.

## Env

- `OPENROUTER_API_KEY` — required for all three. Not in any `.env`
  in the repo; must be in the shell environment. If the user hits
  `RuntimeError: OPENROUTER_API_KEY not set`, point at their shell
  profile — don't try to write it into `.env`.
- `ANTHROPIC_API_KEY` — only needed by `rumil-versus-judge`, not this
  skill.

## Common gotchas

- **Forgetting `--with httpx --with pydantic --with pyyaml`** — the
  versus subproject isn't in rumil's lock, so a bare `uv run python
  versus/scripts/...` fails. The invocation command at the top is
  the canonical form.
- **Stale rows in the UI after a `schema_version` bump** — `status.py`
  catches this. The fix is to re-run `--op paraphrases` + `--op
  completions` to write fresh rows under the new keys; old rows stay
  as archaeology but don't show up in `/versus`.
- **`[skip-refusal]` for several pairs at startup** — that's the judge
  loader filtering out pairs built from earlier refusal rows in
  `completions.jsonl`. Not an error. Those pairs stay pending until
  the refusing model produces a non-refusal completion.

## After a run

Suggest the user check `/versus/results?essay=<id>` (via the running
rumil frontend) to see the new rows, or `/versus/inspect?essay=<id>`
for pair-level breakdown with reasoning. Both pages filter by the
same staleness gate `status.py` reports, so they should agree.
