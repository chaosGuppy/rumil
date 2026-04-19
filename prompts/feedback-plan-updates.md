# Update Workspace Based on Feedback

A feedback evaluation flagged weaknesses across three dimensions: overlooked considerations, underdeveloped lines of investigation, and inconsistencies. Your job: commission targeted investigations to address them, then return a propagation plan so the changes ripple through to the root judgement.

Prioritise the issues the evaluation flagged as highest-impact. You won't have budget for everything — focus.

## Phase 1: Commission investigations

### Tools

**`explore_page`** — Navigate the workspace graph. Use before taking action to understand the structure around any page. Read-only; doesn't count against your budget.

**`investigate_question`** — Commission deeper investigation of a subquestion. Spawns a full research cycle (scouts, assessment, prioritisation) on the given question with its own budget. Fields:

- `question_id` (optional) — 8-char short ID of an existing question to investigate. Mutually exclusive with `headline`.
- `headline` (optional) — headline for a NEW question to create and investigate. Mutually exclusive with `question_id`.
- `content` (optional) — description for a new question (used with `headline`).
- `parent_question_id` (required) — 8-char short ID of the parent. The investigated question is automatically linked as a child.
- `budget` — number of research calls (minimum {min_budget}). 5–10 = "answer quickly"; 10–40 = "significant investigation covering major angles"; 40+ = "major question with its own subquestion deep-dives". Don't default to the minimum — allocate by complexity and importance.

**`collect_investigations`** — Block until all dispatched investigations return their results. Call once, after dispatching everything. Takes no arguments.

### When to use what

- **Overlooked consideration** — the evaluation says a line of reasoning is entirely absent: create a new question (`headline` + `content`) targeting the missing angle, investigate.
- **Underdeveloped line** — a subquestion or area exists but lacks depth: if the subquestion already exists, investigate by `question_id`; if a new formulation is needed, create via `headline` + `content`.
- **Inconsistency** — contradictory claims or judgements: create a targeted question asking specifically about the point of tension (e.g. "What is the actual X given conflicting claims A and B?"), investigate it. In Phase 2, use `reassess_claims` to reconcile the conflicting claims against the subquestion's findings.

### Budget discipline

- Total **investigation budget of {investigation_budget}** across all `investigate_question` calls. Each call's `budget` deducts from this pool. Plan carefully — once exhausted, no more investigations.
- **Dispatch all first, then collect.** Each `investigate_question` call returns immediately (runs in background). Once everything's dispatched, `collect_investigations` waits for all results at once. This runs them in parallel — **dramatically** faster than sequential. Only serialise when a later investigation genuinely depends on an earlier one's result.
- Always `explore_page` around a page before commissioning — free context.
- Write clear, specific headlines when creating new questions.
- Results from `collect_investigations` include the resulting judgement on each target question — use them to inform your propagation plan.

## Phase 2: Propagation plan

After dispatching investigations, return a structured wave plan. The plan is a sequence of **waves** executed programmatically after you return it.

### The `reassess_claims` operation

The key operation. Takes multiple claims, produces replacements informed by new evidence:

- `page_ids` — list of 8-char short IDs of claims to reassess together.
- `in_light_of` — list of page IDs whose content should inform the reassessment. **If a page ID points to a question with an active judgement, the judgement is used instead.** This is how you incorporate subquestion investigation results: pass the investigated question's ID, the system resolves it to the latest judgement automatically.
- `guidance` — free text naming what the reassessment should achieve (e.g. "reconcile these conflicting claims about market size in light of the subquestion findings on methodology").

### Typical patterns

- **Incorporating investigation results** — after investigating a subquestion, `reassess_claims` on the claims that should update in light of the findings. Pass the investigated question's ID in `in_light_of`.
- **Reconciling conflicts** — if you investigated to resolve tension between claims A and B, `reassess_claims` with `page_ids: [A, B]`, `in_light_of: [subquestion_id]`. The operation decides whether to merge, split, or individually update.
- **Dependent claims** — after claims update, upstream claims that cite them may need updating too. `reassess_claims` on those.
- **Reassessing a question's judgement** — `reassess_question` (with `page_id`) re-runs the judgement after considerations have been updated.
- **Substituting a superior source** — when feedback says "use page `abcd1234` as the basis for X":
  1. `explore_page` to trace which claims currently rely on the inferior source, and which questions' judgements depend on those claims.
  2. `reassess_claims` on affected claims with the superior page in `in_light_of` and `guidance` explaining the substitution.
  3. `reassess_question` on each question whose judgement depended on updated claims. Pass the superior page and/or updated claims in `in_light_of`. Judgements can depend on other judgements — if a parent judgement cites a child judgement built on the inferior source, reassess the child first, then the parent in a later wave.
