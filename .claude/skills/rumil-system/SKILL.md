---
name: rumil-system
description: Background knowledge for working with the rumil research workspace from Claude Code. Explains the two-lane provenance model (rumil-mediated vs cc-mediated), when to use which rumil-* skill, how workspace/session state works, and the visibility/attribution story. Auto-load this whenever the user asks about rumil, wants to inspect research, dispatch a call, discuss a question, apply moves, or review a trace.
user-invocable: false
---

# rumil-system — working with rumil from Claude Code

Rumil is an LLM-powered research workspace in this repo. Users pose
questions; rumil investigates them by dispatching structured calls that
produce pages (claims, questions, judgements, concepts, views) linked
into a research graph. See `CLAUDE.md` for the architecture.

This skill is background knowledge. It loads whenever the user is doing
rumil-related work in Claude Code so you don't need to rediscover these
patterns every time.

## Two lanes, clearly marked

Every workspace mutation made from Claude Code falls into one of two
lanes. **You must be clear which lane you're in** — both in your
conversation with the user and in the trace record.

### Rumil-mediated lane

A normal rumil call (`find_considerations`, `assess`, `scout_*`,
`web_research`, `prioritize`) fired via `/rumil-dispatch`. The call
goes through rumil's full pipeline: carefully-scoped context builder,
rumil prompts, rumil tools, bounded LLM agent loop. Claude Code is just
the trigger — the model inside the call sees a tight rumil prompt, not
the broader CC conversation.

**Tags:**
- `runs.config.origin = "claude-code"`
- `runs.config.skill = "rumil-dispatch"`
- `calls.call_params.origin = "claude-code"`
- `runs.config.git_head` records the code state the run used

**Use when:** the user wants real research progress — more
considerations, a proper assessment, a scout pass, web research.

### CC-mediated lane

Claude Code is the brain. You (Claude) decide from your conversation
context — which is *much* broader than any rumil prompt — that a
specific move should happen. The move is applied directly via
`apply_move.py` onto a `CallType.CLAUDE_CODE_DIRECT` envelope Call.
There is *no* rumil-internal LLM call involved; the envelope exists
purely to give the move a well-defined owner in the trace.

**Tags:**
- Call type is `CLAUDE_CODE_DIRECT` (unique to this lane)
- `calls.call_params.origin = "claude-code"`
- `calls.call_params.envelope = true`

**Use when:** mid-conversation the user decides they want a specific
mutation right now (add a subquestion, link two pages, flag a page,
mark a duplicate) and there's no value in running a full rumil call to
mint it. `/rumil-ask` and `/rumil-clean` use this lane.

### Why the split matters

A future reviewer looking at a claim needs to know whether it came from
a careful rumil assess call or from a Claude Code conversation where
the context might have been sprawling. The CallType + call_params tags
make this unambiguous. Respect the split.

## View pages

A `view` page is a curated, sectioned summary of a question's current
understanding — produced by the `create_view` call and updated as
research progresses. Two related page types and three link types show
up alongside:

- `view_item` — an atomic claim/observation inside a View, scored with
  credence/robustness like any page.
- `view_meta` — priority/annotation/proposal notes about a View or a
  specific view_item. Not epistemically scored; carries a `meta_type`.
- `VIEW_ITEM` link (view → view_item) — carries `importance` (1-5),
  `section`, and `position`. Importance lives on the link because an
  item's role can differ across Views.
- `VIEW_OF` link (view → question) — this view covers that question.
- `META_FOR` link (view_meta → view_item or view) — meta annotation.

When these page types or link types appear in trace output, subtrees,
or punch lists, read them as "the question's distilled view," not as
ordinary judgements. A question with a View is meant to be understood
through the View first; dig into the considerations only when the View
is silent or you need to verify it.

## Orchestrators

Rumil has multiple research-loop orchestrators. The two main shapes:

- **Call-graph orchestrators** (`two_phase`, `experimental`) —
  prioritize sub-questions, dispatch a sequence of typed rumil calls
  (`find_considerations`, `assess`, `scout_*`, `web_research`), each
  call going through its own `ContextBuilder` → `WorkspaceUpdater` →
  `ClosingReviewer` pipeline. Budget unit: integer call count. This is
  what `main.py --continue` runs.
- **Spine orchestrators** (`simple_spine`, `axon`) — a single
  persistent agent thread (the "spine") that decides what to do next
  in-thread, dispatching parallel sub-investigations and synthesising
  results. Budget unit: USD, enforced at per-model rates (cache writes
  included). Driven by YAML presets under each orchestrator's
  `configs/` directory.

### Axon specifics

Axon is the newer spine orchestrator and has a few load-bearing
concepts worth knowing when interpreting an axon run:

- **Two-step delegate dispatch.** The mainline thread emits
  `delegate(intent, inherit_context, budget_usd, n)` tool calls. Each
  is followed by a `configure` continuation in the same thread that
  produces a structured `DelegateConfig` (system prompt, tools, max
  rounds, finalize schema, side effects). The orchestrator then runs
  each delegate's *inner loop* with that config; inner loops terminate
  by calling the universal `finalize` tool. All parallel delegates
  gather before the next mainline turn.
- **Continuation vs. isolation regimes.** `inherit_context=True` reuses
  the spine's system + tools + messages so the inner loop hits cache on
  the spine prefix; configure cannot customise system or tools in this
  regime. `inherit_context=False` starts the inner loop fresh and
  configure picks any system / tools.
- **Page creation lives inside delegates.** Workspace pages
  (claims, judgements, view_items, etc.) are minted via the
  `create_page` tool *inside* a delegate's inner loop, not as a side
  effect of finalizing. This means the spine sees a delegate's pages
  only via its finalized `page_ids` field (or via `load_page`).
- **Artifacts vs. pages.** Artifacts are run-local text-by-key state
  (`ArtifactStore`) — useful for content that doesn't fit the
  workspace graph and shouldn't be visible outside the run. Pages are
  the durable workspace-graph nodes. Operating assumptions land at the
  reserved artifact key `OPERATING_ASSUMPTIONS_KEY`.
- **Seed pages + auto-seed.** `OrchInputs.seed_page_ids` are surfaced
  in the spine's first user message as `id + type + headline` (the
  spine calls `load_page` for full content). When `seed_page_ids` is
  empty and `AxonConfig.auto_seed_enabled` is True, the orchestrator
  embeds the question and seeds with the top matches; failures fire an
  `AxonAutoSeedFailedEvent` and the run continues with no seeds.
- **Tiny stable mainline tool surface.** Just `delegate`, `configure`,
  `finalize`, `load_page` (plus configured direct tools). Stable across
  the run so the spine cache prefix never invalidates.

## The rumil-* skill surface

Direct skills (run scripts immediately, no LLM turn needed):

- `/rumil-workspace` — show/list/set the active workspace
- `/rumil-list` — list root questions in the active workspace
- `/rumil-show <qid>` — render a question's subtree, embedding
  neighbors, and recent calls
- `/rumil-search <query>` — embedding search over the workspace
- `/rumil-trace <call_id>` — dump a call's full trace and LLM exchanges
  verbatim

Model-mediated skills (you interpret intent, then act):

- `/rumil-dispatch <call_type> <qid>` — fire one rumil call
  (rumil-mediated lane)
- `/rumil-review <qid>` / `/rumil-clean <qid>` — audit research and
  apply accreting-only fixes (cc-mediated lane)

## Session state

`.claude/state/rumil-session.json` holds:

- `workspace` — the active workspace for this CC session. Every skill
  defaults to this. Override per-call with `--workspace`.
- `chat_envelope` — the active CLAUDE_CODE_DIRECT envelope Call (if
  any). Used by `apply_move` to group cc-mediated mutations.

You can read this file directly if you need to know the current state.

## Running scripts directly

All skill scripts live in `.claude/lib/rumil_skills/` and can be run
outside their SKILL.md wrappers when needed:

```bash
PYTHONPATH=.claude/lib uv run python -m rumil_skills.<script_name> [args]
```

Scripts: `workspace`, `list_questions`, `show_question`, `search_workspace`,
`trace`, `dispatch_call`, `chat_envelope`, `apply_move`.

## Gloss page and call IDs

Whenever you cite a page, call, or any other rumil entity by its short
ID (8-char hex), include a brief gloss: `be6d1a1d (the
AI-governance-determines-space-allocation claim)`, not just `be6d1a1d`.
Bare IDs are opaque — forcing the user to switch to the frontend or
run another skill to know what you're pointing at slows every
discussion. Apply this across every rumil skill's output.

## Visibility and attribution

- **Trace URLs**: every script that creates a run prints the trace URL
  first. Surface it to the user so they can open the rumil frontend
  alongside CC.
- **Terse logging**: scripts print one line per significant event. When
  you relay skill output to the user, keep it scannable — don't paraphrase
  the trace URL away.
- **Git state**: every run records the sha at invocation time, so later
  reviews can correlate a run to the exact code that produced it. Key
  name differs by lane: cc-mediated uses `runs.config.git_head`;
  rumil-mediated uses `runs.config.git_commit` (set by
  `Settings.capture_config()`).
- **Local-only by default**: every script refuses `--prod` unless
  `RUMIL_ALLOW_PROD=1` is set in the shell. Don't try to bypass this.

## One-off DB queries

To inspect the workspace DB directly (e.g. checking what was written to
`runs.config`), use this pattern — `DB.create` is async, the sync client
lives on `db.client`, and `_execute` adds retry/backoff:

```python
import asyncio
from rumil.database import DB

async def main():
    db = await DB.create(run_id="scratch", prod=False, staged=False)
    res = await db._execute(
        db.client.table("runs").select("id,config").limit(10)
    )
    print(res.data)

asyncio.run(main())
```

### Schema cheat sheet

Snapshot of load-bearing columns for the tables you'll query most.
This is here so you don't burn round-trips guessing column names and
hitting `column "X" does not exist`. If you add or rename a column,
update this section in the same change.

#### Common short-name traps (read this before writing a query)

Columns that have an obvious-looking short name that **does not**
exist on the actual table. Picking the short name will fail with a
Postgres `42703` error after a full Python boot — wasteful.

- `calls.cost` ❌ → use **`calls.cost_usd`** (float, dollars)
- `calls.params` ❌ → use **`calls.call_params`** (jsonb)
- `runs` has **no** cost / status / lifecycle columns. Per-run cost
  comes from summing `calls.cost_usd` for the run; there is no
  `started_at` / `finished_at` / `cost_usd_cents` to query.
- `pages.short_id` ❌ — there is no short id column; do `.id[:8]` in
  Python or accept the full UUID
- `versus_texts.run_id` / `project_id` ❌ — `versus_texts` has
  neither; it's workspace-global and dedup'd by `request_hash`

Postgres operator traps:
- `.like('id', '<prefix>%')` on a uuid column → `operator does not
  exist: uuid ~~ unknown`. Fix: fetch the candidate set with a coarse
  filter (e.g. `eq('project_id', ...)`) and do prefix-matching in
  Python, or cast the column with a select expression.
- `.in_(big_list)` on uuid columns: works, but URL-encode quirks
  cap practical list size around ~500 ids per call.

**`runs`** — one row per run (orch run, dispatch, versus job, etc.)
- `id`, `name`, `project_id`, `question_id`, `created_at`
- `config` (jsonb — shape depends on lane; see "config shape" below)
- `staged` (bool)
- No lifecycle columns: status / started_at / finished_at / cost_usd_cents
  do not exist on this branch. Cost rolls up from `calls.cost_usd`;
  liveness from `calls.status` / `calls.completed_at`.

**`calls`** — one row per LLM call dispatched within a run
- `id`, `call_type`, `status`, `run_id`, `project_id`, `workspace`
- `parent_call_id`, `scope_page_id`, `context_page_ids`
- `budget_allocated`, `budget_used`, `cost_usd` (float — **dollars**)
- `created_at`, `completed_at`
- `call_params` (jsonb), `result_summary`, `review_json`, `trace_json`
- `primary_prompt_hash`, `primary_prompt_name`, `sequence_id`, `sequence_position`
- `call_type` lives here, **not** on `runs`.

**`pages`** — every page in the graph
- `id`, `page_type`, `layer`, `workspace`, `project_id`, `run_id`
- `content`, `headline`, `abstract`, `sections`, `extra`
- `credence`, `robustness`, `credence_reasoning`, `robustness_reasoning`
- `importance`, `meta_type`, `task_shape`, `fruit_remaining`
- `epistemic_status`, `epistemic_type`
- `provenance_model`, `provenance_call_type`, `provenance_call_id`
- `superseded_by`, `is_superseded`, `is_human_created`, `hidden`
- `staged` (bool — use with `run_id` for staged-run isolation)

**`page_links`** — typed edges between pages
- `id`, `from_page_id`, `to_page_id`, `link_type`, `direction`, `role`
- `strength`, `reasoning`, `impact_on_parent_question`
- `importance`, `section`, `position` (used by VIEW_ITEM links)
- `run_id`, `staged` (same isolation pattern as `pages`)

**`mutation_events`** — append-only log of mutations to existing state
- `id`, `run_id`, `event_type`, `target_id`, `payload`, `created_at`
- See "Staged Runs and the Mutation Log" in `CLAUDE.md`.

**`page_ratings`**, **`page_flags`** — per-page eval/flag rows
- Both have `run_id`, `staged`, `created_at`, `note`
- Ratings: `page_id`, `call_id`, `score`
- Flags: `flag_type`, `page_id`, `page_id_a`, `page_id_b`, `call_id`

**`projects`** — workspace-as-isolation-boundary
- `id`, `name`, `created_at`, `hidden`, `owner_user_id`
- `--workspace <name>` resolves to a `projects.id` via
  `db.get_or_create_project(name)`.

**`budget`** — per-run budget counter (one row per run)
- `run_id`, `total`, `used`

#### versus tables

**`versus_texts`** — essay completions / paraphrases (workspace-global)
- `id`, `essay_id`, `kind` (completion | paraphrase | human),
  `source_id` (model id, or `orch:<workflow>:<model>:<hash>`)
- `prefix_hash`, `request_hash`, `model_config_hash`
- `model_id`, `request`, `response`, `text`, `params`, `response_words`
- **No `project_id`, no `run_id`.** Dedup is on `request_hash`.

**`versus_judgments`** — pairwise verdicts (per-project)
- `id`, `essay_id`, `prefix_hash`, `criterion`, `variant`
- `source_a`, `source_b`, `text_a_id`, `text_b_id`, `display_first`
- `judge_model`, `request`, `response`, `judge_inputs`, `judge_inputs_hash`
- `verdict`, `preference_label`, `winner_source`, `reasoning_text`,
  `contamination_note`, `duration_s`
- `project_id`, `run_id`, `rumil_call_id`, `rumil_question_id`,
  `rumil_cost_usd`
- **Decoding a row**: read `winner_source` + `preference_label`,
  not `verdict` — `verdict` is relative to display order. See
  `versus/AGENT.md` "Judging contract" for the full breakdown.

#### `runs.config` shape

Differs by lane:
- cc-mediated: `{origin, skill, cc_session, git_head}`
- rumil-mediated: fields from `Settings.capture_config()` —
  `model`, budgets, `git_commit`, `available_calls`, …
- versus: `{origin: "versus", staged, essay_id, task_name, workflow,
  workspace, completion_config | judge_config}`

## When NOT to use these skills

- For running a full orchestrator (multi-call investigation with
  prioritization), prefer `uv run python main.py --continue <qid>
  --budget N` — the orchestrator can spread budget across many calls,
  which `/rumil-dispatch` cannot.
- For bulk batch work, use `main.py --batch`.
- For A/B testing configs or branches, use `scripts/ab_branch.sh`,
  which runs both arms in git worktrees and then kicks off an evaluation.
