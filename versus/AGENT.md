# versus

Pairwise LLM eval harness on longform web essays: models continue essay openings, then blind / workspace-aware / orchestrated judges compare continuations across criteria. The artifact is a gen-model × judge-model matrix of how often each judge prefers the human continuation.

Sources plug in as per-source fetchers under `versus/src/versus/sources/` (currently forethought, redwood, carlsmith). Each produces `Essay` objects with a namespaced id `<source>__<slug>` so dedup keys stay unique across sources. Config lists sources in `essays.sources` with per-source `max_recent` and optional `max_images` / `max_image_ratio` filters; `essays.exclude_ids` blocks specific essays from the pipeline.

Library lives at `versus/src/versus/`; CLI entry points at `versus/scripts/`; cached essays in `versus/data/essays/`. Bridge to rumil is `src/rumil/versus_bridge.py` — exposes `judge_pair_orch` (full `TwoPhaseOrchestrator` per pair + closing call). API routes under `/versus` live in `src/rumil/api/versus_router.py`. Skill for invoking versus from Claude Code: `.claude/skills/rumil-versus-judge/`. (Historical `rumil:ws:*` rows — from an earlier single-agent-call path — are preserved in `versus_judgments` and continue to render through the read-side parsers; no new ws rows are produced.)

## Storage

Two Postgres tables, accessed through `versus.versus_db`:

- `versus_texts` — every essay-shaped contestant: human baselines and model continuations (paraphrase support is deferred, see below). Columns include `request` / `response` (raw provider-shaped JSONB for the API call that produced the text), `text` (extracted continuation), and a generated `request_hash` over the canonical request body.
- `versus_judgments` — pairwise verdicts. `request` / `response` capture the blind judge's literal API call; `judge_inputs` is the canonical condition blob built by `make_versus_config` (see `## Judge config + dedup` below for the structured shape: `workflow.*` / `task.*` subdicts plus cross-cutting top-level fields). `text_a_id` / `text_b_id` are folded in at write time so re-judging different completion samples forks naturally. `judge_inputs_hash` is generated. `winner_source` is generated from `verdict` + `display_first` + `(source_a, source_b)`.

JSONL archives still live under `versus/data/*.jsonl` as a frozen historical record; nothing in the current pipeline reads them. The `versus.jsonl` helper module is dormant — the deferred paraphrase code path imports it, nothing else.

## Core invariant: reruns are content-addressed

Adding a new model / judge / criterion / prefix-config doesn't re-run existing matching rows — same canonical inputs hash to the same `request_hash` (texts) or `judge_inputs_hash` (judgments).

| Table | Dedup key (content-addressed) |
|---|---|
| `versus_texts` | `(essay_id, kind, source_id, prefix_hash, request_hash)` |
| `versus_judgments` | `(essay_id, prefix_hash, source_a, source_b, criterion, judge_inputs_hash)` |

The hash columns are generated server-side from the JSONB blob (sha256 over canonical-form JSON), so editing a prompt template, switching a sampling param, or changing any code that the `code_fingerprint` covers naturally forks the hash and produces a new row. **No manual `*_PROMPT_VERSION` constants** — that footgun is gone.

There's no DB-level uniqueness on either table. "Skip if exists" semantics live in the runner via `versus_db.find_texts` / `find_judgments` queries — runners that want one sample per config skip when a match exists; runners that want N replicates at the same config (e.g. temperature>0 sampling) just insert. Set the policy in the runner, not the schema.

## Judge config + dedup

`versus.versus_config.make_versus_config(workflow, task, ...)` builds the structured config blob (lands in `versus_judgments.judge_inputs` / `versus_texts.params.config`). The legacy `make_judge_config(variant, ...)` API still exists as a back-compat shim that constructs a workflow + task pair internally and forwards into the same builder — keep using `make_versus_config` directly in new code. The display `judge_model` string is `<task.name>/<workflow.name>:<model>:c<hash8>` (e.g. `judge_pair/two_phase:claude-opus-4-7:c2937f03b`, `judge_pair/blind:claude-haiku-4-5:cabcd1234`); it's purely cosmetic and doesn't drive dedup. Historical rows may carry the older `<path>:<model>:<dim>:c<hash8>` shape (`<path>` ∈ {`blind`, `rumil:orch`, `rumil:ws`}); read-side parsers (`mainline.parse_judge_components`, `judge_config_is_current`) accept both.

The blob has two component subdicts plus cross-cutting top-level fields:

- `workflow.*` — `kind` (e.g. `blind`, `two_phase`, `draft_and_edit`), `budget`, plus a snapshot of every entry in the workflow's `relevant_settings`.
- `task.*` — `kind` (e.g. `judge_pair`), `dimension`, `prompt_hash`, `tool_prompt_hash`, `pair_surface_hash`, `closer_hash` (some fields elided per task; the blind shim's task only carries `dimension` + `prompt_hash`).
- top-level: `model`, `model_config` (nested ModelConfig snapshot — sampling, thinking, effort, max_thinking_tokens, service_tier), `workspace_id`, `workspace_state_hash`, plus the post-#425 split `shared_code_fingerprint` (harness layer — files every versus run touches) + `workflow_code_fingerprint` (the workflow's declared `code_paths`). The legacy flat `code_fingerprint` is still accepted from the shim path for hash compat.

claude-* models route direct to Anthropic; everything else through OpenRouter.

At write time, the runner adds `text_a_id` / `text_b_id` (and `order` for rumil rows) before computing the hash. Adding a new "thing the LLM saw" is one place: extend the relevant `task.fingerprint()` / `workflow.fingerprint()`, or `make_versus_config` for cross-cutting fields, plus the matching axis in `project_config_to_axes` so the provenance panel surfaces it. Historical-shape projection (`_project_legacy_config_to_axes`) is preserved for old rows; new fields land in `_project_new_config_to_axes`.

## Per-model registry

`versus/config.yaml` `models:` is the source of truth for what each model gets on the wire — `sampling.temperature`, `sampling.max_tokens`, `sampling.top_p`, `thinking`, `effort`, `max_thinking_tokens`, `service_tier`. Validator on Config catches typos: every model used by `completion.models` / `judging.models` must have a registry entry.

`versus.model_config.get_model_config(model_id, cfg=cfg)` resolves the entry into a `rumil.model_config.ModelConfig`. `get_judge_model_config(model_id, cfg=cfg)` is the same with `cfg.judging.max_tokens` layered on top — judges typically need more output headroom than completion-purpose calls. Both are read everywhere downstream: completions, paraphrases, blind/orch judges, the staleness detector, and `mainline.current_values_summary` for the provenance panel.

Editing a registry entry forks `request_hash` / `judge_inputs_hash` deterministically, so prior data stays valid as the prior config and topup runs land fresh rows under the new condition. `models[<id>].sampling.max_tokens` ignored on direct-Anthropic claude-opus-4-7 because `temperature` is null — the wire kwargs builder drops null fields.

## Sources, unified

Every contestant is a row in `versus_texts` with `kind ∈ {human, completion}` and a uniform `source_id`:
- `human` — the held-out remainder (one row per essay × prefix variant)
- `<model_id>` — from-scratch continuation by a completion model

**Paraphrase support is deferred.** Whole-essay paraphrasing + slicing was the previous strategy for a style-controlled baseline; we may bring it back asking models to paraphrase the *post-prefix* portion directly (in which case it's just another `kind='completion'`), or restore the whole-essay-then-slice path with a `kind='paraphrase'` + `derived_from_id` FK. The dormant code under `paraphrase.py` and the `versus.jsonl` helper survive for that reason. Existing paraphrase JSONL data is archived in `versus/data/paraphrases.jsonl`; 1304 historical paraphrase-touching judgments stay in `versus/data/judgments.jsonl` recoverable.

## Judging contract

Judges reason freely; we parse the **last** 7-point preference label from the output. Both backends (blind, `rumil:orch`) share this parser via `versus.judge.parse_verdict_from_label` → `rumil.versus_prompts.extract_preference`. Don't constrain the whole response to JSON — chain-of-thought materially improves judgment quality.

Display order (A vs B) is deterministic per `(essay_id, sorted_pair)` via `judge.order_pair()` so every judge sees the same assignment for the same pair. The judgment row records `display_first` directly, and `judge_inputs.order` ∈ {`ab`, `ba`} encodes how display order maps onto canonical (alphabetical) `source_a`/`source_b` order. The order is folded into `judge_inputs`, so the same pair judged in both orientations forks the hash and lands as two rows.

## Blind judging

Source ids can literally be `"human"`, so any surface the judge sees (prompt, Question page headline, Question page content, `page.extra` which renders verbatim via `rumil.context.format_page`) must not disclose them. Test coverage is in `tests/test_versus_bridge.py`. Raw source ids stay in the judgment row for post-hoc analysis only.

`essay_id` is also kept off agent-visible surfaces. Its namespaced form `<source>__<slug>` (see `versus.essay.namespaced_id`) bakes the source into what looks like a neutral id and would leak through headline embeddings, `search_workspace`, and `load_page` output. The Question headline uses `prefix_hash[:8]` as a source-free audit tag; `page.extra` drops `essay_id` entirely. Correlation from a trace back to the essay goes through `runs.config.essay_id` (operator-visible, non-agent-visible) or the judgment row's `essay_id` keyed by `question_id`.

**`runs.config` vs `page.extra` — intentional divergence.** `runs.config` for `orch` runs is surfaced in the traces UI but **not** fed to the agent (agent reads pages via `load_page` / `search` / `explore_subgraph`; it never reads the runs row). It's the operator-facing identifying layer: holds `essay_id`, `canonical_source_first` / `canonical_source_second` (which can literally be `"human"`), judge identity, etc. `page.extra` is the stricter blind layer — nothing source-identifying. The two are intentionally NOT blind-equivalent today. If a future refactor routes `runs.config` into agent context, it must first be scrubbed to match `page.extra`'s blindness.

**Canonical vs display order.** Two different A/B conventions coexist, easy to confuse:

- `canonical_source_first` / `canonical_source_second` in `runs.config`, and `source_a` / `source_b` in the judgment row, are the **alphabetical** dedup-key order (`sorted([x, y])`). They don't tell you which side the judge saw as "Continuation A".
- `display_first` on the judgment row is the actual display order. The `judge_inputs.order` field (`ab` or `ba`) encodes how display order maps onto canonical order; `judge.order_from_display_first` computes it.

**When reading a row to learn who won, always go through `winner_source` (and `preference_label` for strength).** A third axis layered on the previous two is `verdict ∈ {A, B, null}`, which records the judge's pick *relative to display order* — `A` means the judge preferred whatever was shown as "Continuation A" (= `display_first`). Combining `verdict` with `display_first` and `(source_a, source_b)` mentally to recover the actual winner is fragile; the easiest way to flip your interpretation of every result in a batch is to read `verdict='A'` and assume `source_a` won. The DB already does the resolution: `winner_source` is a generated column carrying the literal winning side (a model id, the string `"human"`, `"tie"`, or null for refusals/unparsed), and `preference_label` is the parsed 7-point strength (`"A strongly preferred"`, `"B somewhat preferred"`, etc.). Read those.

## Project / run linkage for orch judgments

Judgments produced inside a rumil orchestrator call carry soft references back to rumil:

- `project_id` (uuid, nullable) — set when the judgment is from a rumil project
- `run_id` (text, nullable) — the rumil run that produced the verdict; backed by `runs.id`
- `rumil_call_id` (text, nullable) — the specific call within the run that emitted the verdict

These are all soft references (no FK) so versus stays standalone for blind work and survives if the rumil run gets pruned. (Historical ws rows in the table also have these fields populated.)

## Running

```bash
cd versus
uv venv && uv pip install -e .
export OPENROUTER_API_KEY=...   # required for OpenRouter-based runs
export ANTHROPIC_API_KEY=...    # required for rumil-style judges

# Local Supabase must be running for any storage path:
supabase start                  # one-time

uv run scripts/fetch_essays.py
uv run scripts/run_completions.py                    # single-shot completions (one LLM call per essay × prefix × model)
uv run scripts/run_completions.py --orch two_phase \
    --workspace <ws> --model <id> --budget 4         # orch-driven completions; lands as source_id=orch:<workflow>:<model>:c<hash8>
uv run scripts/run_completions.py --orch simple_spine \
    --workspace <ws> --model <id> --budget-tokens 200000  # SimpleSpine uses --budget-tokens (raw cap), not --budget
uv run scripts/run_rumil_judgments.py                # blind judges (default); --variant orch / simple_spine for the tool-using paths
```

Env resolution for `ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY` cascades: `versus/.env`, then `<rumil-root>/.env`, then process env. Files override process env.

UI routes (`/versus` redirects to `/versus/results`; `/versus/inspect` and `/versus/results` are the real pages) mount in the rumil Next.js frontend; API endpoints in `src/rumil/api/versus_router.py` read versus_texts / versus_judgments and translate to a legacy-shaped envelope so the frontend types didn't have to change.

## Completion variants

`scripts/run_completions.py` has two modes:

- Default — single-shot: one LLM call per essay × prefix × model. Rows land with `source_id=<model_id>` (the bare model id).
- `--orch <workflow_name>` — fires a rumil workflow against a per-essay Question via `versus.rumil_completion.run_orch_completion`, then a closing call extracts the continuation (or, for workflows where `produces_artifact=True`, the runner reads `question.content` directly). Rows land with `source_id=orch:<workflow>:<model>:c<hash8>` so judges can pair orch outputs against single-shot or human baselines. Requires `--workspace`. Pickable as a contestant by `run_rumil_judgments.py` with no further wiring — different workflows / configs / budgets all coexist as separate source_ids by design (the `c<hash8>` suffix is the workflow's `config_hash`).

`WORKFLOW_REGISTRY` in `versus/src/versus/rumil_completion.py` is the source of truth for which workflows `--orch` accepts. Today: `two_phase` (`TwoPhaseWorkflow`, #426), `draft_and_edit` (`DraftAndEditWorkflow`, #427 — drafter → N parallel critics → editor, `produces_artifact=True`), and `simple_spine` (`SimpleSpineWorkflow` — mainline + subroutines self-paced against a token clock). Workflow-specific knobs (e.g. `n_critics`, `max_rounds`, `drafter_model` for `draft_and_edit`; `config_name` for `simple_spine` — defaults to `essay_continuation` on completion / `judge_pair` on judging) are passed via `--workflow-arg key=value` (repeatable, type-coerced from the workflow class's `__init__` signature). Adding a new workflow is one diff: implement the Workflow protocol (`rumil.versus_workflow`), register it in `WORKFLOW_REGISTRY`, update the docs.

### Budget flag: research calls vs. raw tokens

Two camps, mutually exclusive at the CLI:

- **Research-call budget** (`--budget N`) — small int, counts dispatched rumil calls. Used by `two_phase` (min 4), `draft_and_edit`, `claim_investigation`, `experimental`. The orchestrator decides what each call is.
- **Token budget** (`--budget-tokens N`) — raw token cap on the run; the only hard terminator. Used by `simple_spine` (the only token-budget workflow today, listed in `TOKEN_BUDGET_WORKFLOWS` in `rumil_completion.py`). SimpleSpine has no budget-unit primitive — its mainline self-paces against the token clock — so the CLI takes tokens directly to keep the units unambiguous.

The CLI rejects the wrong flag for the chosen workflow (`--budget` on `simple_spine` errors; `--budget-tokens` on non-spine workflows errors). Same split applies to `run_rumil_judgments.py`: `--variant orch` requires `--budget`; `--variant simple_spine` requires `--simple-spine-budget-tokens`.

## Judge variants

`scripts/run_rumil_judgments.py` has two modes. See `.claude/skills/rumil-versus-judge/SKILL.md` for the detailed invocation guide, cost estimates, and confirmation thresholds.

- Blind (default, no `--variant`) — single-turn LLM call using the blind shell + dimension prompt. Repeat `--model` for multi-model runs; each model routes by id (claude-* direct to Anthropic, others via OpenRouter). `judge_model = judge_pair/blind:<canonical_model>:c<hash8>` (no embedded dimension — it lives in `judge_inputs.task.dimension`). Defaults to `cfg.judging.models`.
- `--variant orch` — full TwoPhaseOrchestrator run + closing call per pair. `judge_model = judge_pair/two_phase:<model>:c<hash8>`. Requires local Supabase. Expensive. Uses `--budget N` (research-call count).
- `--variant reflective` — read → reflect → verdict (3 LLM calls, no orch). `judge_model = judge_pair/reflective:<model>:c<hash8>`. Budget flag ignored.
- `--variant simple_spine` — SimpleSpine workflow under the `judge_pair` preset (mainline + subroutines, self-paced). `judge_model = judge_pair/simple_spine:<model>:c<hash8>`. Uses `--simple-spine-budget-tokens N` (raw token cap); `--budget` is rejected.

The earlier `--variant ws` path (one SDK agent call with workspace-exploration tools, no orchestrator) was removed; a low-budget orch run subsumes the agentic-baseline use case. Historical `rumil:ws:*` and `rumil:orch:<model>:<dim>:c<hash8>` rows are preserved and the read-side parsers accept both shapes.

Model for orch is passed explicitly through the bridge (`--model opus|sonnet|haiku|<full-id>`, default opus) — do not rely on `settings.model`. The bridge uses `override_settings(rumil_model_override=model)` to propagate to nested rumil calls.

## Known quirks

- Images not parsed from essay HTML; screen-reader "Image" labels filtered explicitly. Per-source parsers count images pre-strip and store on `Essay.image_count` so the fetch filter can skip image-heavy posts quantitatively.
- Forethought essays end with a `Footnotes` heading + acknowledgement paragraph; both stripped at fetch time by the shared `essay.clean_blocks`.
- Forethought uses KaTeX to render math (MathML + LaTeX annotation + styled HTML all in one span). Parser keeps only the `$latex$` annotation so math reads cleanly.
- Substack (redwood) has two footnote variants — old `<sup>[N]</sup>` + trailing `<ol>`, new `<a class="footnote-anchor">` + `<div class="footnote">` bodies. Both are stripped in `sources/redwood.py`.
- Length tolerance is a prompt hint, not a hard constraint. Some models consistently undershoot — real signal, not to silently correct.
- `essay.SCHEMA_VERSION` invalidates the essay JSON cache. Raw HTML stays cached separately so we don't re-download.
- Refused / content-filtered completions are excluded from pair enumeration (see `judge.is_refusal`).
- Re-imports auto-run a Sonnet validator (`validate_essay.py`) that blocks the import on scraping artifacts (nested-list duplication, orphan footnote digits, caption leakage, etc.). Verdicts cache to `data/essays/<id>.verdict.json` keyed on essay content hash + `VALIDATOR_VERSION`. Pass `--no-validate` to bypass, `--revalidate` to re-run on cached essays.
- Verdict can be `NULL` on judgment rows — refusals or unparsed responses produce a row with `reasoning_text` but no extractable A/B/tie. Aggregations filter on `verdict IS NOT NULL`; the inspector still surfaces the row.

## State staleness

Essay re-imports change `prefix_hash` (because `_content_hash(essay)` is folded in via `prepare.py`). Existing rows in `versus_texts` / `versus_judgments` keep their old hashes — they aren't deleted, but they no longer match what `prepare()` would produce today. Topup runs against stale rows judge OLD essay text, not the current one.

Run before any topup or new judging work:

```bash
uv run python scripts/status.py
```

Exits 2 with a `STALE` banner when cached rows don't match current essays. To regenerate against current essays, run `run_completions.py` + `run_rumil_judgments.py` in order — old rows stay in the DB as historical record; new rows write under the new keys.

Edits to any judge-side prompt or covered-code path auto-fork `judge_inputs_hash` (and therefore the dedup key); no manual version bump.
