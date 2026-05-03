---
name: rumil-versus-generate
description: Run versus's generation scripts — completions and paraphrases — via versus/scripts/run_{completions,paraphrases}.py. Each model in the config routes to its provider (OpenRouter for non-claude ids today; claude-* may route direct to Anthropic in future iterations). Handles the uv --with dance and --config / path anchoring. Default scope is the canonical active essay set; pass --include-stale to widen. Use when the user wants to produce or top up versus completion/paraphrase rows. **Judgments are not in this skill** — the unified judge entry point lives in rumil-versus-judge.
allowed-tools: Bash, Read
argument-hint: "--op completions|paraphrases [--model <id>...] [--essay <id>...] [--include-stale] [--limit N] [--current-only] [--dry-run]"
---

# rumil-versus-generate

Wraps the two versus generation scripts:

| `--op` | Script | What it writes |
|---|---|---|
| `completions` | `run_completions.py` | rows in `versus_texts` — model continuations + a human baseline row per essay |
| `paraphrases` | `run_paraphrases.py` | rows in `data/paraphrases.jsonl` (paraphrase pipeline is currently dormant — kept for legacy invocation) |

For judgments, use **`rumil-versus-judge`**. Its default (no `--variant`)
runs the unified blind path that routes claude-* direct to Anthropic
and everything else through OpenRouter, so there is no longer an
OpenRouter-only judgment script.

## When to use

| Intent | This skill? |
|---|---|
| "run a flash / gpt-5.4 / gemini completion on essay X" | yes |
| "regenerate paraphrases after a prompt bump" | yes |
| "top up judgments (any model)" | **no** — use `rumil-versus-judge` |
| "run a rumil:ws / rumil:orch judge" | **no** — use `rumil-versus-judge` |
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

## Active-set default and `--include-stale`

Both scripts default to the canonical eval set: current
`schema_version` and not in `cfg.essays.exclude_ids` — the same gate
`/versus` applies in the UI (currently a 25-essay set).

Pass `--include-stale` to widen scope to every essay returned by the
source fetchers (still minus `exclude_ids`). This pulls in off-feed
or older-schema rows — useful for backfill or debugging, but those
won't surface in `/versus`.

Default scope composes with `--essay` (AND): `--essay forethought__broad-timelines`
means "that essay, only if it's also in the active set". With
`--include-stale`, `--essay` is the only filter.

## Invocation

Both scripts resolve paths relative to `versus/` regardless of cwd.
Run from the rumil repo root:

```!
cd /Users/brian/code/rumil && uv run --with httpx --with pydantic --with pyyaml python versus/scripts/run_$OP.py $ARGUMENTS
```

The `--with` flags are versus's runtime deps (it's a separate
pyproject, not installed in rumil's `.venv`). If you forget them the
scripts fail with `ModuleNotFoundError: httpx` or similar.

## Typical invocations

**Flash completion across the active set** (the canonical 25-essay
set is the default):

```
--op completions --model google/gemini-3-flash-preview
```

**Top up paraphrases** (when prompt is bumped or new essays land):

```
--op paraphrases
```

Default configured models come from `versus/config.yaml` `completion.models`
(flash + gpt-5.4-mini + claude variants). Each entry has a `paraphrase: bool`
flag — paraphrases run only on the subset where `paraphrase: true`. The
old top-level `paraphrasing.models` list was removed; `paraphrasing` now
only carries `enabled: bool`.

Override with `--model` (repeatable).

## Filter flags

- `--essay <id>` (repeatable) — restrict to specific essays.
- `--include-stale` — opt out of the active-set default; widen to every fetched essay (still minus `exclude_ids`).
- `--limit N` — cap number of calls.
- `--dry-run` — print the plan and exit.

## What to surface

Both scripts print:
- `[plan] N ... calls (concurrency=K)` — count, use before confirming cost.
- `[done i/N] <dedup_key>` — one line per successful row.
- `[skip] <key>` — dedup hit, nothing written.
- `[err ] <key>: <msg>` — failure; other items still run.

## Cost

OpenRouter pricing varies by model; rough per-call costs for this
workflow's default max_tokens settings:

| Model | Completion |
|---|---|
| google/gemini-3-flash-preview | ~$0.002 |
| openai/gpt-5.4-mini | ~$0.01 |
| openai/gpt-5.4 | ~$0.05 |

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

Both keys are needed depending on which models run:

- `OPENROUTER_API_KEY` — required for any non-claude model
  (gemini, gpt-5.4, etc.). Routed via OpenRouter.
- `ANTHROPIC_API_KEY` — required for any `claude-*` / `anthropic/...`
  model. Both `run_completions.py` and `run_paraphrases.py` apply
  the env cascade (versus/.env, then rumil/.env, then process env),
  matching what `run_rumil_judgments.py` does, so per-project `.env`
  files take precedence over the shell env.

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
  `versus_texts`. Not an error. Those pairs stay pending until the
  refusing model produces a non-refusal completion.

## After a run

Suggest the user check `/versus/results?essay=<id>` (via the running
rumil frontend) to see the new rows, or `/versus/inspect?essay=<id>`
for pair-level breakdown with reasoning. Both pages filter by the
same staleness gate `status.py` reports, so they should agree.
