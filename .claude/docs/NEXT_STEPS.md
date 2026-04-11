# rumil-* skills: next steps and visions

Living planning document for the Claude Code skill system that drives
rumil from CC. Updated as ideas land, ship, or get dropped.

Last major update: after committing `f389003` (increment 2 — inspection,
review, and clean skills + apply_move friction fixes).

## Where we are now

Two commits in, the spine is functionally complete:

| Category | Skills |
|---|---|
| **Read / inspect** | `rumil-list`, `rumil-show`, `rumil-page`, `rumil-search`, `rumil-trace`, `rumil-workspace` |
| **Triage** | `rumil-find-confusion` (heuristic + `--deep`) |
| **Discuss** | `rumil-chat`, `rumil-review` |
| **Act (rumil-mediated)** | `rumil-dispatch` |
| **Act (cc-mediated)** | `rumil-clean`, `apply_move` (via envelope) |
| **Iterate** | `rumil-prompt-edit` |
| **Meta / subagents** | `rumil-system`, `rumil-researcher`, `rumil-explorer` |

Both provenance lanes are wired and traceable. The `CLAUDE_CODE_DIRECT`
envelope records `MovesExecutedEvent`s with hydrated PageRefs, so
cc-mediated mutations are visible in the rumil frontend exactly like
rumil-internal calls. Scan log persists confusion verdicts so `--deep`
scans amortize. `open_run` captures `git_head` so runs are
code-state-aware.

The things we're explicitly *not* doing yet:
- No remote / prod database access (gated behind `RUMIL_ALLOW_PROD=1`)
- No rumil-side schema changes beyond the one new `CallType` enum value
- No automated chaining between skills (the user is the orchestrator)
- No write-back of CC chat summaries into rumil

## Design principles to preserve

These have paid off so far and should keep shaping future work:

1. **Two lanes, clearly marked.** Every mutation is either rumil-mediated
   (a real rumil call with carefully-scoped prompt, `origin=claude-code`
   tag on `runs.config`) or cc-mediated (direct move on a
   `CLAUDE_CODE_DIRECT` envelope). Never blur this line. The trace UI
   should always be able to tell a reviewer which lane a given page came
   from.

2. **Local-first by default.** Every script refuses `--prod` unless
   `RUMIL_ALLOW_PROD=1` is set in the shell. This isn't cosmetic — it's
   the protection against Claude deciding to `--prod` something by mistake.

3. **Attribution and visibility.** Every run gets an immediate trace URL,
   `git_head` captured, `cc_session` stamp. Short IDs get glossed in any
   model-mediated output. Claude Code's conversation surface should make
   it take ≤10 seconds to notice when something went wrong.

4. **Accreting by default in cc-mediated work.** The allowlist on
   `apply_move --accreting-only` means `rumil-clean` and `rumil-chat`
   build up workspace state but can't destroy or mutate in place.
   Destructive moves require explicit opt-out.

5. **Skills compose through the user.** One skill, one purpose. Chaining
   is the user's job. This keeps each skill's body small and its behavior
   inspectable. When skills start needing to know about each other,
   that's a smell worth investigating before adding magic.

6. **Friction = fix.** When Claude or the user hits a wall constructing
   inputs, interpreting outputs, or doing a cross-skill dance by hand,
   that's a signal to build a small, scoped fix. `apply_move --schema`
   is the canonical example: ~100 lines, removes a recurring failure
   mode. See the friction audit section.

## Near-term: small, actionable fixes

These are each ~1-2 hours of work, independent, and directly remove
friction that already exists.

### N1. `rumil-runs` skill

**What:** A direct skill listing recent runs across the active
workspace — run_id, name, question (short ID + gloss), origin
(cc / cli / ab), budget used/total, total cost, trace URL.

**Why:** `rumil-list` is question-centric and `rumil-find-confusion` is
call-centric. There's no "what's this workspace been up to operationally"
view. Right now the only surface for that is the rumil frontend.

**Shape:** New script `runs.py` that queries the `runs` table joined with
question headlines, filtered by project. Small SKILL.md wrapping it.

**Size:** 1-2 hours.

### N2. Fold scan-log verdicts into `rumil-show`

**What:** When `rumil-show` prints the "recent calls on this question"
tail, look up each call id in the scan log and prepend an indicator if
there's a verdict: `[confused s3]`, `[ok]`, `[—]`.

**Why:** The scan log is rich data we produce and then only expose in
`rumil-review`. Surfacing it on `rumil-show` means any inspection of a
question immediately shows whether the producing calls are suspect.

**Shape:** One import + one DB-free lookup per call row. ~20 lines.

**Size:** 30 minutes.

### N4. `rumil-prompt-edit` — script-backed file lookup

**What:** Replace the hardcoded call_type → prompt-file mapping table in
`rumil-prompt-edit`'s SKILL.md with a tiny helper script that introspects
`rumil.llm.build_system_prompt` (or reads `prompts/` directly) to return
the file(s) used for a given call type. The skill body calls the helper
instead of hardcoding.

**Why:** The table drifts when prompts are added, renamed, or
restructured. Introspection is authoritative.

**Shape:** `prompt_file_for.py` helper + SKILL.md edit.

**Size:** 1 hour.

### N5. `apply_move --envelope-status` shortcut

**What:** Running `apply_move` with no args could print envelope status
instead of usage (or alongside it). More discoverable than having to know
about `chat_envelope status`.

**Why:** The two scripts are conceptually one surface. Users (and
Claude) shouldn't have to know they're separate binaries.

**Size:** 30 minutes.

### N6. `show_page` link graph traversal depth

**What:** Currently `show_page` shows one-hop links. A `--hops N` flag
would show N-hop neighborhoods (with cycle protection per the repo
convention in `context.py`).

**Why:** Chasing a chain ("what's linked to what's linked to this claim")
currently requires multiple invocations.

**Size:** 1 hour.

### N7. Dispatch call from punch list by referenced action

**What:** When `rumil-review` produces a punch list with
`suggested: dispatch <call_type>`, the user currently has to copy the
call_type + question_id into a separate `/rumil-dispatch` invocation.
Could be one step.

**Shape:** Either extend `rumil-review` to include a copy-pasteable
command block per item, or build a small `rumil-act-on-punchlist` that
takes the textual output and walks it. The first option is simpler.

**Size:** 30 minutes for the first option.

### N8. `rumil-find-confusion --question <qid>` filter

**What:** Scope confusion scanning to calls on a single question. Right
now it's workspace-wide only.

**Why:** Natural pairing with `rumil-review <qid>` — you want to triage
the calls on the question you're reviewing, not the whole workspace.

**Size:** 30 minutes.

### N9. Drop the throwaway test pages from the workspace

**What:** Manually delete `68df0eee`, `50671cfc`, and the test envelope
pages left over from smoke tests during increment 1 and 2 development.

**Why:** They're noise. They showed up in `rumil-list` and will keep
doing so until cleaned up.

**Size:** 5 minutes.

## Medium-term: capabilities worth building

Each of these is half-day to day-plus. They unlock new workflows rather
than polishing existing ones.

### M1. `rumil-cc-activity` — the envelope history view

**What:** A skill that summarizes recent CC-mediated work — list every
`CLAUDE_CODE_DIRECT` envelope call in the workspace, with the count of
moves applied, the scope question, the session timestamp, the
suggested action outcomes. "What has CC been doing to this workspace?"

**Why:** Makes the two-lane distinction a *visible, queryable thing*
instead of only a design principle. Right now the envelope log is rich
data sitting mostly idle. A human reviewer can see that a given claim
was created by CC, but only by clicking into a specific trace — there's
no birds-eye view.

**Shape:** New script. Queries `calls` where `call_type =
'claude_code_direct'`, joins with pages created via those envelopes,
aggregates move types. Direct skill wrapping it.

**Bonus:** Could add a `--revert <envelope_id>` that proposes
un-doing a cc-mediated session (via the mutation log / staged runs
pattern). Ambitious but the staged-runs infrastructure is already there.

**Size:** Half-day for the read-only view, day+ for the revert path.

### M2. Scan-log-aware everything

**What:** Beyond N2, push scan log integration deeper:
- `rumil-list` shows a count of calls with active confused verdicts per
  question
- `rumil-trace` flags whether the call has a scan verdict at the top
- `rumil-dispatch` warns if a recent call of the same type on the same
  question has a confused verdict ("heads up — last `find_considerations`
  on this question is flagged as confused; re-running without fixing the
  root cause may reproduce the issue")

**Why:** The data exists; the system just isn't using it. Each surface
makes the triage loop tighter.

**Size:** Half-day.

### M3. Cross-skill chaining (opt-in)

**What:** Selected skills can *offer* to chain. `rumil-review` ends with
"4 items suggest prompt edits — want me to walk them as a prompt-edit
session?" and on yes, pivots into `rumil-prompt-edit` for each in turn.
`rumil-find-confusion` ends with "top candidate is call X on question Y —
want me to /rumil-trace it?".

**Why:** The user becoming the orchestrator between every pair of skills
is friction. But automatic chaining is risky — it hides decisions. A
middle ground: *suggested* chains that require explicit consent, so the
user stays in the loop but the cost is one "yes" instead of
copy-paste-rerun.

**Shape:** This is mostly a SKILL.md / conversation pattern change, not
new code. The pattern: end each model-mediated skill's output with an
explicit "suggested next" block that names a concrete command. Claude
then offers to run it if appropriate.

**Size:** Half-day to get the pattern right across the existing
model-mediated skills.

### M4. Session write-back as rumil source pages

**What:** When a meaningful `rumil-chat` or `rumil-review` session ends,
offer to write the session transcript (or a summary of it) as a rumil
source page via the ingest move, scoped to the question being discussed.

**Why:** cc-mediated sessions currently evaporate. The work Claude and
the user did together informs claims and flags but the reasoning isn't
captured anywhere rumil can see. Writing it back makes the reasoning
discoverable by future rumil calls (via embedding search) and by future
human review.

**Caveats:**
- Requires defining what "meaningful" means (length? # of moves made?)
- Transcripts can contain sensitive / low-signal chatter — maybe summary
  not full transcript
- Same trap as auto-narration: easy to over-produce noise pages

**Shape:** New script `ingest_session.py` that creates a source page with
the conversation content (or an LLM-summarized version) and links it to
the scoped question. SKILL.md guidance for when to offer this.

**Size:** Day.

### M5. `rumil-health` workspace view

**What:** Aggregate view of workspace state: how many questions have
stale judgements (judgement created before N considerations), how many
calls are flagged confused, which call types have the highest confusion
rate, budget burn by run.

**Why:** Triage at the workspace level, not the question level. Signals
"this workspace needs attention" or "the assess call is getting
confused a lot recently — probably a prompt regression".

**Shape:** New script + skill. Reads the same tables everything else
does. Could eventually drive prompt-edit priorities.

**Size:** Day.

### M6. `rumil-explorer` subagent actually used by `rumil-review`

**What:** Right now `rumil-review` loads context synchronously in the
main conversation. For large questions this burns tokens on loading
before Claude even starts reviewing. Alternative: have `rumil-review`
delegate subtree exploration + call-trace gathering to the
`rumil-explorer` subagent, which does the crawl and returns a compact
report. Main conversation stays lean.

**Why:** Scales to bigger questions. Also lets the explorer subagent
iterate on its own (follow interesting links) without cluttering the
user's main context.

**Shape:** `rumil-review` SKILL.md gets an agent-delegation mode
(maybe as the default for large questions).

**Size:** Half-day.

## Longer-term visions (speculative; need more real use first)

These are exciting but shouldn't be built *now* — they need a base of
accumulated usage to know what's actually wanted.

### V1. Prompt A/B as a first-class workflow

Today `rumil-prompt-edit` → commit → `rumil-dispatch` → compare traces
manually. Vision: `rumil-prompt-ab <before_call_id> <after_call_id>`
shows a side-by-side diff of the two calls' outputs + traces, maybe with
a meta-LLM verdict on whether the second is better. Requires capturing
the `(before, after, prompt_commit_sha)` triple somewhere persistent — a
new lightweight table or stash-in-runs.config.

Premature until we've done 5-10 real prompt iterations and know what
pattern of comparison is actually useful.

### V2. Workspace-level health dashboard (persistent, not on-demand)

Today `rumil-health` (M5) would be on-demand. Vision: a background hook
runs it nightly, stores history in a `health_snapshots` table, the
frontend grows a dashboard tab. Would let you spot "confusion rate on
assess calls has been creeping up for a week" without manual triage.

Needs M5 first + proof that the nightly aggregation is useful.

### V3. CC-driven orchestration

Today the rumil orchestrator (`two_phase`, `claim_investigation`, etc.)
picks what call to dispatch next. Vision: Claude Code *is* an
orchestrator option. Instead of a rumil-internal prioritization call,
Claude reads the workspace state, reasons about it in CC, and fires
targeted dispatches via `rumil-dispatch`. The CC session becomes the
outer loop, rumil calls are the inner work units.

This is philosophically the biggest move in any of these visions because
it inverts the current hierarchy (rumil drives, CC supports). Might be
wrong — rumil's prompts are tuned for this task, Claude Code is not.
But it opens up mixing human judgment, web research, and rumil calls in
ways the current orchestrators can't.

Needs M3 (cross-skill chaining) and M1 (cc-activity history) first, at
minimum.

### V4. Two-way sync: CC chat notes become rumil sources

When a CC session produces insights the user wants to capture, write
them to the workspace as source pages (via ingest), with the CC session
ID + envelope call ID in `extra`. Future rumil calls see them via
embedding search. Future human review of the session can trace back
from source page → envelope → full session transcript.

Related to M4 but more ambitious — more metadata, more reversibility,
maybe a frontend UI for browsing "CC insight sources" distinctly from
user-ingested sources.

### V5. The skill system as a plugin

Package `.claude/` as a distributable plugin so others can install the
rumil-* skills against their own rumil workspace. Requires:
- Pinning shared-lib structure to plugin conventions
- Making `PYTHONPATH=.claude/lib` cleaner (plugin-relative, not
  repo-relative)
- Documenting the rumil version compatibility matrix (which skills need
  which rumil features)
- Decoupling any assumptions about `prompts/` or `src/rumil/` from the
  repo layout

Not urgent. The current setup is fine for solo use and nothing prevents
us from doing this later.

## Frictions to watch for

These are patterns that, when they show up, should trigger a small fix
rather than a grumble. The `apply_move --schema` addition is the
archetype.

- **Claude guessing input shapes and failing validation.** If a script's
  payload has non-trivial structure and the help text doesn't show
  field names, build a `--schema` / `--help-payload` affordance.
- **Short IDs appearing bare in conversation output.** Already caught by
  the memory rule + SKILL.md guidance. Watch for regressions.
- **Manual copy-paste of IDs between skills.** Means either the two
  skills should chain (M3) or the output should be formatted as a
  copy-pasteable command (N7-style).
- **Looking things up in the frontend because CC can't see them.** Means
  there should be a direct skill for that lookup. `rumil-page` was born
  from this pattern; `rumil-runs` (N1) is the next obvious case.
- **Duplicated logic between `.claude/lib/` and `src/rumil/`.** Never
  reimplement what rumil already does — always prefer the rumil-side
  method over a local copy. (N3 was the first instance: `trace.py` had
  its own call-id resolver, now deferred to `db.resolve_call_id`.)
- **"Which file/function am I supposed to look at"** — the prompt-file
  mapping in `rumil-prompt-edit` is the current case. Replace hardcoded
  tables with introspection where possible (N4).
- **Scripts silently missing new enum values.** My `dispatch_call`'s
  scout map duplicates `scripts/run_call.py`'s. Adding a new scout type
  won't break either script — it just won't appear in them. Worth a
  test at some point to assert parity.
- **Skill descriptions drifting from behavior.** If a skill's frontmatter
  `description` no longer matches what the body does, Claude's auto-load
  decisions get unreliable. Periodically re-read the descriptions.

## Open questions

Things I don't know the answer to — worth your input when you get a
chance.

1. **Envelope lifecycle:** should a `CLAUDE_CODE_DIRECT` envelope ever
   be "closed" formally (status set to `complete`, completed_at filled)?
   Currently it stays `pending` forever because nothing marks it done.
   The rumil frontend might render pending calls differently. Options:
   never close (they're session-long), close on `chat_envelope clear`,
   close after N minutes of inactivity.

2. **One envelope per session or per question?** Today: one per CC
   session (keyed roughly on ppid). If you chat about multiple questions
   in one session, all the moves land on one envelope. Alternative:
   envelope per (session, question) pair. More rows but clearer trace
   grouping. Which matches how you think about CC sessions?

3. **Staged vs unstaged for envelopes?** Today envelopes run with
   `staged=False`, so cc-mediated moves are immediately visible to
   other readers. Should they default to staged, so the user can
   review before "committing" a CC session? The staged-runs machinery
   is built for exactly this.

4. **What should `rumil-find-confusion --deep` do for
   `claude_code_direct` envelopes?** Currently skipped via the
   heuristic scorer because there are no LLM exchanges to evaluate.
   Should there be a different quality check for cc-mediated work?
   Something like "did this envelope produce useful mutations or
   mostly noise?"

5. **Meta-model default.** I set `DEFAULT_META_MODEL = "claude-sonnet-4-6"`
   in `llm_helpers.py`. Reasonable for deep confusion scans, but maybe
   different skills want different defaults (prompt-edit might want
   opus for the subtlety, find_confusion might want haiku for bulk).
   Worth a per-skill override, or keep it global?

6. **How should CC sessions be discoverable from the rumil frontend?**
   The envelope is there as a Call, but nothing distinguishes it
   visually from a regular call in the frontend's trace list — except
   the call_type string. Worth a small frontend change to render
   `claude_code_direct` calls with a distinct icon / color?

7. **What counts as "enough use" to start building visions (V1–V5)?**
   I keep saying "wait until we have real usage" but haven't defined
   the threshold. Maybe: 3-5 real review cycles + 10+ cc-mediated
   envelopes + at least one prompt edit that actually shipped?

## What I'd prioritize first

If I had to rank for next session:

1. **Run a real loop end-to-end.** Pick one question in the workspace,
   do `find-confusion --deep` → `trace` → `review` → `clean` or
   `prompt-edit`, and keep a running list of every wall I hit. Commit
   the fixes as I go. This is the highest-leverage thing because it
   generates a stream of targeted fixes that would otherwise take weeks
   of speculation to invent.

2. **N1 (rumil-runs)** and **N2 (scan log in rumil-show)** — each ~30
   minutes to an hour, both genuinely useful, both remove frictions I
   already know exist.

3. **M3 (cross-skill chaining)** — the conversation pattern change, not
   the big infrastructure version. This is the thing that makes the
   skill system feel cohesive instead of a bag of tools, and it's
   mostly SKILL.md edits.

4. Answer the open questions above, at least 1-3.

**Not priorities right now:**

- Visions (V1-V5) — premature, but worth holding
- `rumil-health` (M5) — will be more useful after more accumulated data
- Session write-back (M4) — needs design iteration, we don't know what
  shape the artifact should be yet

---

This doc is meant to be updated. When an item ships, strike it
through. When a new friction appears, add it to that section. When a
vision matures into something concrete, promote it to medium-term.
