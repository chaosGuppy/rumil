# versus

Pairwise LLM eval harness on longform web essays: models continue essay openings ("from-scratch") or paraphrase whole essays (style-controlled baseline), then blind judges compare continuations across criteria. The artifact is a gen-model × judge-model matrix of how often each judge prefers the human continuation.

Sources plug in as per-source fetchers under `versus/src/versus/sources/` (currently forethought, redwood, carlsmith). Each produces `Essay` objects with a namespaced id `<source>__<slug>` so dedup keys across all three jsonl stores stay unique. Config lists sources in `essays.sources` with per-source `max_recent` and optional `max_images` / `max_image_ratio` filters; `essays.exclude_ids` blocks specific essays from the pipeline.

Library lives at `versus/src/versus/`; CLI entry points at `versus/scripts/`; data in `versus/data/`. Bridge to rumil is `src/rumil/versus_bridge.py` — exposes `judge_pair_ws_aware` (single agent call with workspace tools) and `judge_pair_orch` (full `TwoPhaseOrchestrator` per pair + closing call). API routes under `/versus` live in `src/rumil/api/versus_router.py`. Skill for invoking versus from Claude Code: `.claude/skills/rumil-versus-judge/`.

## Core invariant: reruns are free

Adding a model, judge, criterion, or prefix-config must **never** re-run existing matching rows. All three stores are keyed on deterministic dedup keys:

| Store | Key composition |
|---|---|
| `data/completions.jsonl` | `essay_id · prefix_config_hash · source_id · sampling_hash` |
| `data/paraphrases.jsonl` | `essay_id · model_id · sampling_hash` |
| `data/judgments.jsonl`   | `essay_id · prefix_hash · sorted(source_a, source_b) · criterion · judge_model · order` |

`prefix_config_hash` mixes in essay content + prefix params (n_paragraphs, include_headers, length_tolerance) + `prepare.COMPLETION_PROMPT_VERSION`. `sampling_hash` covers sampling params (temperature/max_tokens/top_p) plus — for paraphrases — `paraphrase.PARAPHRASE_PROMPT_VERSION`.

**If you edit a completion/paraphrase prompt template, bump the relevant `*_PROMPT_VERSION` constant.** Editing without bumping leaves old rows keyed as if the prompt hadn't changed — they silently persist.

## Judge prompt versioning

OpenRouter and `anthropic:<model>` judges embed `:p<hash>:v<N>` in their `judge_model` string via `judge.compose_judge_model`. Bump `judge.JUDGE_PROMPT_VERSION` when editing `render_judge_prompt` or `CRITERION_PROMPTS` so existing rows fork instead of silently persisting.

Rumil-style judge_model strings (`rumil:ws:...`, `rumil:orch:...`, `rumil:text:...`) embed four version knobs:

- **`:p<hash>`** — automatic. `versus_bridge.compute_prompt_hash(task_body)` hashes `prompts/versus-judge-shell.md` + the task body (`prompts/versus-<name>.md` or a versus criterion prompt). Any `.md` edit forks the key.
- **`:v<N>`** — manual. `versus_bridge.BLIND_JUDGE_VERSION`. Bump when you make a semantic change the prompt hash and page-surface hash don't catch. **Unhashed surfaces to watch:** the inline user prompts in `judge_pair_ws_aware` / `_run_orch_closer` / `_build_rumil_text_user_message`, `disallowed_tools` config, orchestrator-internal tool set. (The Question page surface — `_build_headline`, `_format_pair_content`, `_versus_extra` key schema — is covered automatically by `:q<hash>` below on ws/orch; bump `:v<N>` when the surface-hash forking isn't enough or you need to fork `rumil:text:*` alongside.) Applies to all three rumil variants.
- **`:t<hash>`** — automatic, ws/orch only. `versus_bridge.compute_tool_prompt_hash()` hashes the `{tool_name: description_string}` map for the workspace-exploration tools (`search_workspace`, `load_page`, `explore_subgraph`). Edits to those tool docstrings fork the key. Does not cover the broader dispatch-call tool set used inside the orchestrator (those fork via `BLIND_JUDGE_VERSION`).
- **`:q<hash>`** — automatic, ws/orch only. `versus_bridge.compute_pair_surface_hash()` hashes the agent-visible Versus Question page surface: the headline template (`_build_headline`), the content body shape (`_format_pair_content`), and the `_versus_extra` key schema. Edits to any of those auto-fork ws/orch keys without forking `rumil:text:*` (which doesn't read the Question page). Computed once per run via a sentinel `PairContext`.
- **`:s<hash>`** — automatic, non-ws paths (OpenRouter / `anthropic:<model>` / `rumil:text`). `judge.compute_sampling_hash(sampling)` hashes the sampling dict (`{"temperature": ..., "max_tokens": ...}`) so topups at a different temperature re-judge instead of silently no-opping.

## Sources, unified

Every "contestant" the judge sees is a row in `completions.jsonl`, with `source_kind ∈ {human, completion, paraphrase}` and a uniform `source_id`:
- `human` — the held-out remainder (written once per essay × prefix_config)
- `<model_id>` — from-scratch continuation by a completion model
- `paraphrase:<model_id>` — derived remainder of a model's full-essay paraphrase (synthesized from `paraphrases.jsonl` at completion-run time; no extra API call)

## Judging contract

Judges reason freely; we parse the **last** 7-point preference label from the output. All three backends (OpenRouter, `anthropic:*`, and `rumil:*`) share this parser via `versus.judge.parse_verdict_from_label` → `rumil.versus_prompts.extract_preference`. Don't constrain the whole response to JSON — chain-of-thought materially improves judgment quality.

Display order (A vs B) is deterministic per `(essay_id, sorted_pair)` via `judge.order_pair()` so every judge — model or human — sees the same assignment for the same pair.

Every row records an `order ∈ {"ab", "ba"}` field (`"ab"` iff the alphabetically-lower source was shown as Continuation A). `order` is also the last slot in `judgment_key`, so the key scheme supports a future mirror-mode that emits both orders per pair and collapses them on the read side to cancel position bias. Today enumeration still emits one task per pair — nothing is doubled by default. Legacy rows that predate the `order` field are handled via `judge.infer_order(row)`, which derives the orientation from the stored `display_first`; any downstream code that needs the per-row order should go through that helper so pre- and post-change rows coexist cleanly.

## Blind judging

Source ids can literally be `"human"`, so any surface the judge sees (prompt, Question page headline, Question page content, `page.extra` which renders verbatim via `rumil.context.format_page`) must not disclose them. Test coverage is in `tests/test_versus_bridge.py`. Raw source ids stay in the judgment row for post-hoc analysis only.

`essay_id` is also kept off agent-visible surfaces. Its namespaced form `<source>__<slug>` (see `versus.essay.namespaced_id`) bakes the source into what looks like a neutral id and would leak through headline embeddings, `search_workspace`, and `load_page` output. The Question headline uses `prefix_hash[:8]` as a source-free audit tag; `page.extra` drops `essay_id` entirely. Correlation from a trace back to the essay goes through `runs.config.essay_id` (operator-visible, non-agent-visible) or the judgment row's `essay_id` keyed by `question_id`.

**`runs.config` vs `page.extra` — intentional divergence.** `runs.config` for `ws`/`orch` runs is surfaced in the traces UI but **not** fed to the agent (agent reads pages via `load_page` / `search` / `explore_subgraph`; it never reads the runs row). It's the operator-facing identifying layer: holds `essay_id`, `canonical_source_first` / `canonical_source_second` (which can literally be `"human"`), judge identity, etc. `page.extra` is the stricter blind layer — nothing source-identifying. The two are intentionally NOT blind-equivalent today. If a future refactor routes `runs.config` into agent context, it must first be scrubbed to match `page.extra`'s blindness.

**Canonical vs display order.** Two different A/B conventions coexist, easy to confuse:

- `canonical_source_first` / `canonical_source_second` in `runs.config`, and `source_a` / `source_b` in the judgment row, are the **alphabetical** dedup-key order (`sorted([x, y])`). They don't tell you which side the judge saw as "Continuation A".
- `display_first` / `display_second` on the judgment row is the actual display order — what the judge rendered as "A" and "B" on the Question page. The `order` field (`ab` or `ba`) encodes how display order maps onto canonical order; `judge.order_from_display_first` computes it.

`display_first` / `display_second` live only on the judgment row, not on `runs.config` or `page.extra`, to keep source identity out of run metadata that's easier to accidentally surface. For a specific pair's display order at audit time, go to `data/judgments.jsonl`.

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

- `text` — single-turn Anthropic call using versus's judge prompt. `judge_model = anthropic:<model>:p<hash>:v<N>:s<hash>`.
- `rumil-text` — single-turn Anthropic call using rumil's dimension prompt (isolates prompt-source effect from workspace/tools effect). `judge_model = rumil:text:<model>:<dim>:p<hash>:v<N>:s<hash>`.
- `ws` — one VERSUS_JUDGE agent call with workspace-exploration tools against a `--workspace`. `judge_model = rumil:ws:<model>:<ws>:<task>:p<hash>:v<N>:t<hash>:q<hash>`. Requires local Supabase.
- `orch` — full TwoPhaseOrchestrator run + closing call per pair. `judge_model = rumil:orch:<model>:<ws>:b<N>:<task>:p<hash>:v<N>:t<hash>:q<hash>`. Requires local Supabase. Expensive.

Model for ws/orch/rumil-text is passed explicitly through the bridge (`--rumil-model opus|sonnet|haiku`, default opus) — do not rely on `settings.model`. The bridge uses `override_settings(rumil_model_override=model)` to propagate to nested rumil calls.

## Known quirks

- Images not parsed from essay HTML; screen-reader "Image" labels filtered explicitly. Per-source parsers count images pre-strip and store on `Essay.image_count` so the fetch filter can skip image-heavy posts quantitatively.
- Forethought essays end with a `Footnotes` heading + acknowledgement paragraph; both stripped at fetch time by the shared `essay.clean_blocks`.
- Forethought uses KaTeX to render math (MathML + LaTeX annotation + styled HTML all in one span). Parser keeps only the `$latex$` annotation so math reads cleanly.
- Substack (redwood) has two footnote variants — old `<sup>[N]</sup>` + trailing `<ol>`, new `<a class="footnote-anchor">` + `<div class="footnote">` bodies. Both are stripped in `sources/redwood.py`.
- Length tolerance is a prompt hint, not a hard constraint. Some models consistently undershoot — real signal, not to silently correct.
- `essay.SCHEMA_VERSION` invalidates the essay JSON cache. Raw HTML stays cached separately so we don't re-download.
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
