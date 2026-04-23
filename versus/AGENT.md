# versus

Pairwise LLM eval harness on forethought.org essays: models continue essay openings ("from-scratch") or paraphrase whole essays (style-controlled baseline), then blind judges compare continuations across criteria. The artifact is a gen-model × judge-model matrix of how often each judge prefers the human continuation.

Library lives at `versus/src/versus/`; CLI entry points at `versus/scripts/`; data in `versus/data/`. Bridge to rumil is `src/rumil/versus_bridge.py` — exposes `judge_pair_ws_aware` (single agent call with workspace tools) and `judge_pair_orch` (full `TwoPhaseOrchestrator` per pair + closing call). API routes under `/versus` live in `src/rumil/api/versus_router.py`. Skill for invoking versus from Claude Code: `.claude/skills/rumil-versus-judge/`.

## Core invariant: reruns are free

Adding a model, judge, criterion, or prefix-config must **never** re-run existing matching rows. All three stores are keyed on deterministic dedup keys:

| Store | Key composition |
|---|---|
| `data/completions.jsonl` | `essay_id · prefix_config_hash · source_id · sampling_hash` |
| `data/paraphrases.jsonl` | `essay_id · model_id · sampling_hash` |
| `data/judgments.jsonl`   | `essay_id · prefix_hash · sorted(source_a, source_b) · criterion · judge_model` |

`prefix_config_hash` mixes in essay content + prefix params (n_paragraphs, include_headers, length_tolerance) + `prepare.COMPLETION_PROMPT_VERSION`. `sampling_hash` covers sampling params (temperature/max_tokens/top_p) plus — for paraphrases — `paraphrase.PARAPHRASE_PROMPT_VERSION`.

**If you edit a completion/paraphrase prompt template, bump the relevant `*_PROMPT_VERSION` constant.** Editing without bumping leaves old rows keyed as if the prompt hadn't changed — they silently persist.

## Judge prompt versioning

OpenRouter and `anthropic:<model>` judges embed `:p<hash>:v<N>` in their `judge_model` string via `judge.compose_judge_model`. Bump `judge.JUDGE_PROMPT_VERSION` when editing `render_judge_prompt` or `CRITERION_PROMPTS` so existing rows fork instead of silently persisting.

Rumil-style judge_model strings (`rumil:ws:...`, `rumil:orch:...`, `rumil:text:...`) embed two version knobs:

- **`:p<hash>`** — automatic. `versus_bridge.compute_prompt_hash(task_body)` hashes `prompts/versus-judge-shell.md` + the task body (`prompts/versus-<name>.md` or a versus criterion prompt). Any `.md` edit forks the key.
- **`:v<N>`** — manual. `versus_bridge.BLIND_JUDGE_VERSION`. Bump when you make a semantic change the prompt hash doesn't catch. **Unhashed surfaces to watch:** `_format_pair_content`, the inline user prompts in `judge_pair_ws_aware` / `_run_orch_closer` / `_build_rumil_text_user_message`, the tool list / `disallowed_tools` config, `_versus_extra` contents. If you change any of those in a way that affects judge behavior, bump `BLIND_JUDGE_VERSION`.

## Sources, unified

Every "contestant" the judge sees is a row in `completions.jsonl`, with `source_kind ∈ {human, completion, paraphrase}` and a uniform `source_id`:
- `human` — the held-out remainder (written once per essay × prefix_config)
- `<model_id>` — from-scratch continuation by a completion model
- `paraphrase:<model_id>` — derived remainder of a model's full-essay paraphrase (synthesized from `paraphrases.jsonl` at completion-run time; no extra API call)

## Judging contract

Judges reason freely; we parse the **last** `<verdict>A|B|tie</verdict>` tag from the output (OpenRouter / anthropic:* variants) or the 7-point preference label (rumil:* variants). Don't constrain the whole response to JSON — chain-of-thought materially improves judgment quality.

Display order (A vs B) is deterministic per `(essay_id, sorted_pair)` via `judge.order_pair()` so every judge — model or human — sees the same assignment for the same pair.

## Blind judging

Source ids can literally be `"human"`, so any surface the judge sees (prompt, Question page headline, Question page content, `page.extra` which renders verbatim via `rumil.context.format_page`) must not disclose them. Test coverage is in `tests/test_versus_bridge.py`. Raw source ids stay in the judgment row for post-hoc analysis only.

`runs.config` for `ws`/`orch` runs is surfaced in the traces UI but **not** fed to the agent (agent reads pages via `load_page` / `search` / `explore_subgraph`; it never reads the runs row). Judge identity and per-pair metadata are safe to embed there.

## Running

```bash
cd versus
uv venv && uv pip install -e .
export OPENROUTER_API_KEY=...   # required for OpenRouter-based runs
export ANTHROPIC_API_KEY=...    # required for rumil-style judges

uv run scripts/fetch_essays.py
uv run scripts/run_paraphrases.py
uv run scripts/run_completions.py   # also synthesizes paraphrase-remainder rows
uv run scripts/run_judgments.py         # OpenRouter judges
uv run scripts/run_rumil_judgments.py   # Anthropic-direct / rumil-bridge judges
```

Env resolution for `ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY` cascades: `versus/.env`, then `<rumil-root>/.env`, then process env. Files override process env.

UI routes (`/versus`, `/versus/judge`, `/versus/inspect`, `/versus/results`) mount in the rumil Next.js frontend; API endpoints in `src/rumil/api/versus_router.py` read the JSONL stores directly. No DB tables.

## Rumil-style judge variants

`scripts/run_rumil_judgments.py` has four `--variant` options. See `.claude/skills/rumil-versus-judge/SKILL.md` for the detailed invocation guide, cost estimates, and confirmation thresholds.

- `text` — single-turn Anthropic call using versus's judge prompt. `judge_model = anthropic:<model>:p<hash>:v<N>`.
- `rumil-text` — single-turn Anthropic call using rumil's dimension prompt (isolates prompt-source effect from workspace/tools effect). `judge_model = rumil:text:<model>:<dim>:p<hash>`.
- `ws` — one VERSUS_JUDGE agent call with workspace-exploration tools against a `--workspace`. `judge_model = rumil:ws:<model>:<ws>:<task>:p<hash>:v<N>`. Requires local Supabase.
- `orch` — full TwoPhaseOrchestrator run + closing call per pair. `judge_model = rumil:orch:<model>:<ws>:b<N>:<task>:p<hash>:v<N>`. Requires local Supabase. Expensive.

Model for ws/orch/rumil-text is passed explicitly through the bridge (`--rumil-model opus|sonnet|haiku`, default opus) — do not rely on `settings.model`. The bridge uses `override_settings(rumil_model_override=model)` to propagate to nested rumil calls.

## Known quirks

- Images not parsed from essay HTML; screen-reader "Image" labels filtered explicitly.
- Forethought essays end with a `Footnotes` heading + acknowledgement paragraph; both stripped at fetch time.
- Length tolerance is a prompt hint, not a hard constraint. Some models consistently undershoot — real signal, not to silently correct.
- `fetch.SCHEMA_VERSION` invalidates the essay JSON cache. Raw HTML stays cached separately so we don't re-download.
- Refused / content-filtered completions are excluded from pair enumeration (see `judge.is_refusal`).
- Re-imports auto-run a Sonnet validator (`validate_essay.py`) that blocks the import on scraping artifacts (nested-list duplication, orphan footnote digits, caption leakage, etc.). Verdicts cache to `data/essays/<id>.verdict.json` keyed on essay content hash + `VALIDATOR_VERSION`. Pass `--no-validate` to bypass, `--revalidate` to re-run on cached essays.

## State staleness

Essay re-imports change `prefix_config_hash` (because `_content_hash(essay)` is folded in via `prepare.py`). Existing rows in `completions.jsonl` / `paraphrases.jsonl` / `judgments.jsonl` keep their old hashes — they aren't deleted, but they no longer match what `prepare()` would produce today. Topup runs against stale rows judge OLD essay text, not the current one.

Run before any topup or new judging work:

```bash
uv run python scripts/status.py
```

Exits 2 with a `STALE` banner when cached rows don't match current essays. To regenerate against current essays, run `run_paraphrases.py` + `run_completions.py` + `run_judgments.py` in order — old rows stay in the jsonls as historical record; new rows write under the new keys.

Same logic applies when `PARAPHRASE_PROMPT_VERSION`, `JUDGE_PROMPT_VERSION`, or `BLIND_JUDGE_VERSION` is bumped — the status check picks up paraphrase staleness via `sampling_hash`; judge-prompt/version bumps surface as a different `judge_model` column in analyze tables.
