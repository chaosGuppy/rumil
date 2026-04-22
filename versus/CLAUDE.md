# versus

Pairwise LLM eval on forethought.org essays. Each essay is split at N paragraphs; models continue from there ("from-scratch"), and each model also paraphrases the whole essay as a style-controlled baseline (human content in the model's voice). Blind pairwise judges compare continuations across multiple criteria. The eventual artifact is a **gen-model × judge-model matrix** of how often each judge prefers the human continuation, faceted by criterion and by condition (completion vs paraphrase).

## Core invariant: reruns are free

Adding a model, judge, criterion, or prefix-config must **never** re-run existing matching rows. All three stores are keyed on deterministic dedup keys:

| Store | Key composition |
|---|---|
| `data/completions.jsonl` | `essay_id · prefix_config_hash · source_id · sampling_hash` |
| `data/paraphrases.jsonl` | `essay_id · model_id · sampling_hash` |
| `data/judgments.jsonl`   | `essay_id · prefix_hash · sorted(source_a, source_b) · criterion · judge_model` |

`prefix_config_hash` mixes in a hash of the cleaned essay content + prefix params (n_paragraphs, include_headers), so changes to normalization or split config invalidate downstream correctly. `sampling_hash` covers temperature / max_tokens / top_p.

Don't break this: any refactor that silently changes prompt text without feeding into a hash produces stale rows. If prompt templates change in a way that should invalidate, bump a version field that feeds the hash — don't just edit the prompt.

## Config-driven

Everything lives in `config.yaml`: essay source, prefix settings, completion models, paraphrase models, judge models, criteria, length tolerance, storage paths, UI port. The three model lists are independent — you can have a model only as a judge, only as a gen-model, or both. Commented templates show how to extend for the full 5-model matrix run.

## Sources, unified

Every "contestant" the judge sees is a row in `completions.jsonl`, with `source_kind ∈ {human, completion, paraphrase}` and a uniform `source_id`:
- `human` — the held-out remainder (written once per essay × prefix_config)
- `<model_id>` — from-scratch continuation by a completion model
- `paraphrase:<model_id>` — derived remainder of a model's full-essay paraphrase (synthesized from `paraphrases.jsonl` at completion-run time; no extra API call)

Downstream (judging, UI) iterates these uniformly.

## Judging contract

Judges reason freely; we parse the **last** `<verdict>A|B|tie</verdict>` tag from the output. Don't constrain the whole response to JSON — chain-of-thought materially improves judgment quality.

Display order (A vs B) is deterministic per `(essay_id, sorted_pair)` so every judge — model or human — sees the same assignment for the same pair.

## Layout

```
src/versus/   # library: fetch, prepare, complete, paraphrase, judge, analyze, ui, openrouter, jsonl, config
scripts/      # thin CLI entry points
src/versus/templates/  # jinja2 for the FastAPI UI
data/         # generated; gitignored. essays/ JSON, *.jsonl for everything else
```

## Running

```bash
uv venv && uv pip install -e .
export OPENROUTER_API_KEY=...   # required for OpenRouter-based runs
export ANTHROPIC_API_KEY=...    # required for rumil-style judges (run_rumil_judgments.py)

uv run scripts/fetch_essays.py
uv run scripts/run_paraphrases.py
uv run scripts/run_completions.py   # also synthesizes paraphrase-remainder rows
uv run scripts/run_judgments.py         # OpenRouter judges
uv run scripts/run_rumil_judgments.py   # Anthropic-direct judges (see below)
uv run scripts/serve_ui.py          # UI at http://127.0.0.1:8765
```

Each script is idempotent. Re-running just fills gaps.

## Rumil-style judges

`scripts/run_rumil_judgments.py` runs pairwise judgments via a direct
Anthropic client (no OpenRouter, no `anthropic` SDK — just httpx). Rumil
itself deliberately uses Anthropic directly for prompts and
orchestration, so its judges do too. Results land in the same
`data/judgments.jsonl` with `judge_model = "anthropic:<model>"` and flow
through to the `/results` UI alongside OpenRouter judges unchanged.

Models are read from `config.yaml` under `judging.anthropic_models`, or
can be passed ad-hoc: `--model claude-opus-4-7 --model claude-sonnet-4-6`.
Also supports `--limit N` and `--dry-run`.

**Env resolution.** `ANTHROPIC_API_KEY` (and `OPENROUTER_API_KEY`) are
resolved from this cascade, highest precedence first:

1. `versus/.env`
2. `<rumil-root>/.env`
3. process environment

Files override the process env so per-project `.env` files take
precedence over stale shell exports. No extra dep — `envcascade.py` has
a 20-line parser. Whichever source supplied each key is reported on
missing-key errors.

### Workspace-aware and orchestrator variants

Beyond the text-only path above, `run_rumil_judgments.py --variant ws`
and `--variant orch` run judgments through rumil's actual machinery:

- **`ws`** — one `VERSUS_JUDGE` rumil agent call per pair, using
  single-arm workspace-exploration tools (`search_workspace`,
  `load_page`, `explore_subgraph`) scoped to a user-chosen workspace.
  Dimension prompts live in `prompts/versus-<name>.md` (essay-adapted
  versions of rumil's run-eval dimensions); default is
  `general_quality`. `--versus-criterion <name>` substitutes a versus
  criterion prompt instead, for direct comparison with OpenRouter
  judges on the same criterion axis. `judge_model` = `rumil:ws:<model>:<ws_short>:<task>`.

- **`orch`** — per-pair Question creation, then
  `TwoPhaseOrchestrator.run()` at configurable budget, then a closing
  call extracts the 7-point label. Produces a full research trace per
  pair. `judge_model` = `rumil:orch:<model>:<ws_short>:b<N>:<task>`.

Both variants:
- Require a running local Supabase (rumil's DB).
- Use rumil's configured `settings.model` (defaults to
  `claude-opus-4-7`) — model is derived from rumil's test/smoke-mode
  flags, not per-call configurable from versus.
- Write back into `data/judgments.jsonl` with extra fields
  (`rumil_trace_url`, `rumil_preference_label`, `rumil_call_id`,
  `rumil_run_id`, `rumil_question_id`, `rumil_cost_usd`) so the
  versus `/inspect` page can link to rumil traces.
- Create Question pages in the chosen workspace tagged
  `extra.source = "versus"` for filterability. Expect some noise in
  that workspace's question list.

See `.claude/skills/rumil-versus-judge/SKILL.md` for invocation, cost
estimates, and confirmation thresholds.

## UI

- `/` — blind human judging (stored alongside model judges as `human:<name>`)
- `/inspect` — essay / completion prompt / judge prompt / paraphrase prompt / all generated sources, side-by-side per essay
- `/results` — gen × judge matrix of %-picks-human, per-criterion small multiples, per-source length sanity (avg words + Δ vs target — watch this; it's a setup-trust signal)

## Known quirks

- Images are not parsed from essay HTML (not in the Markdown-* component set we recognize). Screen-reader "Image" labels inside Markdown-p wrappers are filtered explicitly.
- Forethought essays end with a `Footnotes` heading + an acknowledgement paragraph ("We would like to thank…" / "This article has gone through several rounds of development…"). Both are stripped at fetch time.
- Length tolerance is a prompt hint, not a hard constraint. Some models consistently undershoot (Gemini 3 Flash paraphrases at ~50% of target). That's a real signal, not something to silently correct.
- Schema bumps: `fetch.SCHEMA_VERSION` invalidates the essay JSON cache. Raw HTML stays cached separately so we don't re-download.
