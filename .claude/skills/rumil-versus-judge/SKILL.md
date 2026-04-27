---
name: rumil-versus-judge
description: Run pairwise judgments on versus essay-continuation pairs. Default mode is the unified blind path (single-turn LLM call, no tools, no DB) — claude-* models route direct to Anthropic, others through OpenRouter. --variant ws adds a rumil SDK agent with workspace-exploration tools; --variant orch fires a full TwoPhaseOrchestrator run per pair. Use when the user wants to measure how rumil discriminates on pairs with known ground truth (human continuation vs. model continuations), compare blind judges against workspace-aware ones, or top up pending judgments after adding new dimensions or models.
allowed-tools: Bash, Read
argument-hint: "[--variant ws|orch] [--workspace <name>] [--model opus|sonnet|haiku|<full-id> ...] [--dimension <name>...] [--essay <id>...] [--contestants <csv>] [--vs-human] [--budget N] [--limit N] [--concurrency N] [--current-only] [--persist] [--dry-run]"
---

# rumil-versus-judge

Runs pairwise judgments on versus essay-continuation pairs against a
rumil-adjacent judge backend, writing into the same
`versus/data/judgments.jsonl` that OpenRouter judges use. The versus UI
picks up the new rows automatically; rumil paths also mirror a trace
URL + 7-point preference label + call/run/question IDs so the `/inspect`
page can link back to the rumil trace.

## Three modes

| Mode | What it does | judge_model format | Needs supabase? |
|---|---|---|---|
| Blind (default, no `--variant`) | Single-turn LLM call, no tools, no DB. Each `--model` routed: `claude-*` direct to Anthropic, others via OpenRouter. Same shell/dimension prompt across providers. | `<canonical_model>:<dim>:p<hash>:v<N>:s<hash>` | no |
| `ws` | One VERSUS_JUDGE rumil agent call per pair with single-arm workspace-exploration tools (search / load_page / explore_subgraph). Uses the essay-adapted rumil dimension prompt selected via `--dimension`. | `rumil:ws:<model>:<ws_short>:<task>:p<hash>:v<N>:t<hash>` | **yes** |
| `orch` | Per-pair: create Question page, fire `TwoPhaseOrchestrator` at configurable budget, then a closing call extracts the 7-point label. Produces a full research trace per pair. | `rumil:orch:<model>:<ws_short>:b<N>:<task>:p<hash>:v<N>:t<hash>` | **yes** |

`<dim>` / `<task>` is the rumil dimension name selected via `--dimension`
(e.g. `general_quality`, `grounding`). Blind keys carry the dimension
inline; ws/orch keys carry it as the trailing `<task>` segment. The
blind shell is **without** tool advertisements (no scope-question
references, no `load_page`/`search_workspace` mentions); ws/orch shell
keeps them. Both modes share the same template — see
`rumil/versus_prompts.py` for the substitution dicts.

Four tags appear in the key:

- `:p<hash>` — hash of the *composed* system prompt for this mode. Blind and tools modes hash to different spaces because the composed shell differs.
- `:v<N>` — `BLIND_JUDGE_VERSION` (now gates everything, including blind judges). Bump for semantic changes the prompt hash misses.
- `:t<hash>` (ws / orch only) — hash of the workspace-exploration tool description map.
- `:s<hash>` (blind only) — hash of the sampling dict.

Legacy keys (`anthropic:<model>:p..:v..`, `rumil:text:<model>:<dim>:...`, bare-OR `<provider>/<model>:p..:v..`) still parse for old jsonl rows; they read as stale because they hash the pre-collapse shell.

See `versus/AGENT.md` for the full interaction between these four knobs.

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
`stale_prompt` / `unknown_essay`) are what `/versus/results` and
`/versus/judge` filter on in the UI, so `status.py`'s numbers should
match what you see there.

## Env & config

- `ANTHROPIC_API_KEY` resolves from `versus/.env`, then
  `<rumil-root>/.env`, then the process environment. Files override env
  so per-project `.env` takes precedence.
- All variants take their model via `--model`. Accepts a short alias
  (`opus` / `sonnet` / `haiku`) or a full Anthropic model id.
- `text` variant: repeat `--model` for multi-model runs; defaults to
  `judging.anthropic_models` in `versus/config.yaml`.
- `rumil-text` / `ws` / `orch` variants: pass at most one `--model`
  (default: `opus`). Passed explicitly through the bridge via
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
- `--concurrency N` — concurrent LLM calls. Defaults: `ws` = 2,
  `text` / `rumil-text` = `cfg.concurrency` (usually 8), `orch` = 1
  (serial). Raise `orch` concurrency cautiously (each pair fires a full
  TwoPhaseOrchestrator with lots of DB traffic); 2 is usually fine and
  roughly halves wall time.

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
- `--variant ws --workspace <WS> --model sonnet --limit 5` — run on sonnet instead of opus
- `--variant ws --workspace <WS> --dimension general_quality --dimension grounding --limit 5` — run multiple dimensions
- `--variant orch --workspace <WS> --budget 4 --limit 3` — 3 orch judgments at minimum budget (TwoPhaseOrchestrator rejects budget < 4)

## What to surface

All variants print:
- `[plan] N ... judgments ...` — pending count. Use this before confirming cost.
- `[done i/N] <dedup_key>  label=... trace=<url>` (ws/orch) or `[done ...]` (text)
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

| Variant | Model | $/judgment | 100 judgments |
|---|---|---|---|
| text | sonnet (4-6) | $0.03-0.08 | $3-8 |
| text | opus (4-7) | $0.20-0.40 | $20-40 |
| rumil-text | sonnet (4-6) | $0.03-0.10 (similar to `text`; different prompt) | $3-10 |
| rumil-text | opus (4-7) | $0.20-0.45 | $20-45 |
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
  workspace, dimension (or versus criterion), budget, sampling params
  (via `:s<hash>`), tool-prompt contents (via `:t<hash>` for ws/orch),
  and `order` slot — so any combination change produces fresh rows
  without clobbering existing ones. `order` is currently always
  single-order; the slot exists so mirror-mode can be switched on
  without a migration.
- **Rumil trace UI requires rumil's frontend** (`./scripts/dev-api.sh`
  + `cd frontend && pnpm dev`). Trace URLs emitted by the script point
  at `settings.frontend_url` (default http://127.0.0.1:3000).

## When to bump `BLIND_JUDGE_VERSION`

`BLIND_JUDGE_VERSION` (in `versus/src/versus/versions.py`;
re-exported from `src/rumil/versus_bridge.py` for back-compat)
is the manual version knob that forks `rumil:ws:*`, `rumil:orch:*`,
and `rumil:text:*` judge_model keys when you make a semantic change
the automatic prompt hash doesn't catch. **Bump it when editing any
of:**

- `versus_bridge._format_pair_content` — the Question page body the
  agent reads via `load_page`.
- `versus_bridge._versus_extra` — rendered verbatim in page views.
- The inline user prompts constructed in code:
  - `judge_pair_ws_aware` (agent user prompt + allowed/disallowed tools)
  - `_run_orch_closer` (closer user prompt)
  - `_build_rumil_text_user_message` (rumil-text user message)
- The tool *list* / `disallowed_tools` config on `SdkAgentConfig` in
  `judge_pair_ws_aware` (adding/removing tools) — changing what the
  agent can reach changes its behavior. Tool *docstring* edits are
  already auto-covered by `:t<hash>`; only list changes need a bump.
- Anything else in the bridge that a future reader would consider
  "part of the judge's identity" but isn't an `.md` file on disk.

**Do NOT bump** when editing:

- `prompts/versus-judge-shell.md` or `prompts/versus-<dim>.md` —
  covered by `:p<hash>` automatically. `tests/test_versus_prompt_snapshots.py`
  pins the file shas, so a prompt edit without the corresponding version
  bump fails the snapshot test with a message pointing at the constant
  to roll.
- The sampling dict passed to non-ws paths — covered by `:s<hash>`.
- Tool descriptions/docstrings for `search_workspace` / `load_page` /
  `explore_subgraph` — covered by `:t<hash>` for ws/orch.

When you bump, also update the comment next to the constant with a
short reason, so future readers know what the bump paid for
(e.g. `# v2 (2026-04-23): fixes #3 headline leak and #4 page.extra
leak`).
