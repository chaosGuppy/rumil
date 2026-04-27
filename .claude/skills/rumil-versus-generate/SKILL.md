---
name: rumil-versus-generate
description: Run versus's generation scripts — completions and paraphrases — via versus/scripts/run_{completions,paraphrases}.py. Each model in the config routes to its provider (OpenRouter for non-claude ids today; claude-* may route direct to Anthropic in future iterations). Handles the uv --with dance, --config / path anchoring, and --active. Use when the user wants to produce or top up versus completion/paraphrase rows. **Judgments are not in this skill** — the unified judge entry point lives in rumil-versus-judge.
allowed-tools: Bash, Read
argument-hint: "--op completions|paraphrases [--model <id>...] [--essay <id>...] [--active] [--limit N] [--current-only] [--dry-run]"
---

# rumil-versus-generate

Wraps the two versus generation scripts that write into
`versus/data/*.jsonl`:

| `--op` | Script | What it writes |
|---|---|---|
| `completions` | `run_completions.py` | `completions.jsonl` — model continuations + a human baseline row per essay |
| `paraphrases` | `run_paraphrases.py` | `paraphrases.jsonl` — same-model rewrites of each essay's remainder |

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

## The `--active` flag

Both scripts support `--active` — restrict to the canonical eval set:
current `schema_version` and not in `cfg.essays.exclude_ids`. This is
the 25-essay set `/versus` enumerates.

Without `--active`, the scripts still honor `exclude_ids` (so excluded
essays never get rows), but they'll happily touch older-schema essays
if those are still in the cache. Use `--active` to mirror what the UI
sees — the default for "run flash across the active set" style tasks.

`--active` composes with `--essay`: both act as filters (AND). So
`--active --essay forethought__broad-timelines` means "the essay, only
if it's also in the active set."

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

**Flash completion across the active set** (the 25-essay canonical
set, ~30 API calls at flash rates):

```
--op completions --active --model google/gemini-3-flash-preview
```

**Top up paraphrases** (when prompt is bumped or new essays land):

```
--op paraphrases --active
```

Default configured models come from `versus/config.yaml`:
- `completion.models` → flash + gpt-5.4-mini + claude variants (Anthropic ids routed via OpenRouter)
- `paraphrasing.models` → flash + gpt-5.4-mini + gpt-5.4

Override with `--model` (repeatable).

## Filter flags

- `--essay <id>` (repeatable) — restrict to specific essays.
- `--active` — the 25-essay canonical set.
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
