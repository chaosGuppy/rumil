---
name: rumil-versus-judge
description: Run rumil-style pairwise judgments on versus essay-continuation pairs. Versus is the standalone gen-model × judge-model matrix over forethought.org essays; this skill adds Anthropic-direct judges (the rumil judge backend) into that matrix so we can compare rumil's discrimination against OpenRouter judges on pairs where ground truth (the human continuation) is known. Use when the user wants to (a) benchmark rumil's judging on known-answer pairs, (b) compare Anthropic judges against OpenRouter judges in versus, or (c) top up pending judgments after adding new Anthropic models.
allowed-tools: Bash, Read
argument-hint: "[--model <id>...] [--limit N] [--dry-run]"
---

# rumil-versus-judge

Runs pairwise judgments on versus essay-continuation pairs using
Anthropic as the judge backend. This is the cheap v0 of rumil-adjacent
judging — each pair × criterion × model becomes a single Anthropic call,
results written to `versus/data/judgments.jsonl` with
`judge_model = "anthropic:<model>"`. The workspace-aware variant (agent
with tools against a rumil workspace) is deferred — see
`versus/CLAUDE.md` for the plan.

## When to use

| Intent | Skill |
|---|---|
| "run rumil judges on versus pairs" / "compare Anthropic judges in versus" | this skill |
| "run OpenRouter judges (Gemini/GPT) on versus pairs" | `versus/scripts/run_judgments.py` directly — not this skill |
| "A/B eval two rumil research runs against each other" | rumil's `main.py --ab-eval A B` — different system |

Versus must already have completions cached (`versus/data/completions.jsonl`).
If it doesn't, the user needs `versus/scripts/fetch_essays.py` +
`run_paraphrases.py` + `run_completions.py` first — flag that and stop.

## Env & config

- `ANTHROPIC_API_KEY` is resolved from `versus/.env`, then
  `<rumil-root>/.env`, then the process environment. Files override env
  so per-project `.env` takes precedence.
- Anthropic models come from `versus/config.yaml` under
  `judging.anthropic_models` by default. `--model` overrides.
- Criteria come from `config.yaml` under `judging.criteria` (same as
  OpenRouter judges — same prompts, same dedup logic).

## Invocation

```!
cd /Users/brian/code/rumil && uv run --with httpx --with pydantic --with pyyaml versus/scripts/run_rumil_judgments.py $ARGUMENTS
```

Versus has its own `pyproject.toml` and isn't installed in rumil's
`.venv`, so we pass its runtime deps explicitly to `uv run --with`.

## What to surface

The script prints:
- `[plan] N anthropic judgment calls (concurrency=K)` — how many pending
- `[done i/N] <dedup_key>` — per-judgment completion
- `[err ] <key>: <msg>` — per-judgment failure (the run continues)
- `[info] no pending anthropic judgments` — everything already cached

After it runs, suggest the user open the versus UI (`cd versus &&
uv run scripts/serve_ui.py` → http://127.0.0.1:8765/results) to see the
updated gen × judge grid.

## Cost confirmation

**Always `--dry-run` first** and **confirm with the user before firing if
the expected cost is > ~$10.** Rough per-call estimates (versus judge
prompt, ~3k input tokens, ~2-5k output tokens typical):

| Model | $/call (rough) | 100 calls | 500 calls |
|---|---|---|---|
| claude-opus-4-7 | $0.20-0.40 | $20-40 | $100-200 |
| claude-sonnet-4-6 | $0.03-0.08 | $3-8 | $15-40 |

So: >~50 opus calls or >~200 sonnet calls warrants explicit confirmation.
The plan line (`[plan] N anthropic judgment calls`) tells you N; use the
table above to estimate total. If the user has named a budget, stay
within it; if they haven't and the count is large, offer a `--limit`
option and wait for their go-ahead.

## Other caveats

- **No rumil workspace access yet.** This v0 is a direct Anthropic call
  with versus's judge prompt — structurally the same shape as an
  OpenRouter judge, different model axis. The interesting test (does
  workspace context help discriminate?) needs the workspace-aware
  variant, which isn't built.
- **Re-running is free.** Dedup key format matches the OpenRouter path,
  so re-running just fills gaps.
