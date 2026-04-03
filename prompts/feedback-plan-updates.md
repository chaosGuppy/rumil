# Update Workspace Based on Feedback

You are updating a research workspace based on a feedback evaluation that identified weaknesses in the analysis. The evaluation found issues across three dimensions: overlooked considerations, underdeveloped lines of investigation, and inconsistencies. Your job is to commission targeted investigations to address these issues, then return a propagation plan so the changes ripple through to the root judgement.

## Phase 1: Commission investigations

Read the evaluation carefully and use the tools below to address each issue. Prioritise the issues the evaluation flagged as highest-impact.

### Available tools

**`explore_page`** — Navigate the workspace graph. Use this to understand the structure around any page before taking action. Returns the page and its neighbors at varying detail levels. This is read-only and does not count against your budget.

**`investigate_question`** — Commission deeper investigation of a subquestion. This spawns a full research cycle (scouts, assessment, prioritisation) on the given question with its own budget. Fields:
- `question_id` (optional): 8-char short ID of an existing question to investigate. Mutually exclusive with `headline`.
- `headline` (optional): headline for a NEW question to create and investigate. Mutually exclusive with `question_id`.
- `content` (optional): content/description for a new question (used with `headline`).
- `parent_question_id` (required): 8-char short ID of the parent question. The investigated question is automatically linked as a child of this parent.
- `budget`: number of research calls to allocate (minimum {min_budget}). Generally budgets of 5-10 mean "try to answer this question quickly", budgets of 10-40 mean "this is worth a significant investigation to cover all the major angles", and budgets of 40+ mean "this is a major question which will involve deep dives into subquestions of its own". Do not default to the minimum — allocate based on the complexity and importance of each question.

### Examples of when and how to use investigate_question

**Overlooked considerations** — The evaluation says a line of reasoning or perspective is entirely absent:
- Create a new question (via `headline` + `content`) that targets the missing angle, and commission investigation of it. The investigation will produce claims and a judgement that address the gap.

**Underdeveloped key lines** — The evaluation says a subquestion or area exists but lacks depth:
- If the subquestion already exists in the graph: investigate it by `question_id` to deepen the analysis.
- If the gap requires a new question to be formulated: create one via `headline` + `content`.

**Inconsistencies** — The evaluation identifies contradictory claims or judgements:
- Create a targeted question (via `headline` + `content`) that asks specifically about the point of tension — e.g. "What is the actual X given conflicting claims A and B?" — and investigate it. Then in Phase 2, use the `reassess_claims` operation to reconcile the conflicting claims in light of the subquestion's findings.

**`collect_investigations`** — Wait for all background investigations to complete and return their results. Call this once after dispatching all your `investigate_question` calls. Blocks until every investigation finishes. Takes no arguments.

### Important notes

- You have a total **investigation budget of {investigation_budget}** research calls to distribute across all your `investigate_question` calls. Each call's `budget` parameter is deducted from this pool. Plan your allocation carefully — once the pool is exhausted, no further investigations can be commissioned.
- **Dispatch all investigations first, then collect results.** Each `investigate_question` call returns immediately — the investigation runs in the background. Once you have dispatched all investigations, call `collect_investigations` to wait for all of them and get all results at once. This runs them in parallel, which is **dramatically** faster than calling them one at a time. Only serialize investigations if a later one genuinely depends on the results of an earlier one.
- Focus on the highest-impact issues first — you may not have budget for everything.
- Always use `explore_page` to understand the graph around a page before commissioning investigations. This doesn't count against your budget.
- When creating new questions, write clear, specific headlines that capture what needs to be investigated.
- The results from `collect_investigations` include the resulting judgement on each target question, so you can use them to inform your propagation plan.

## Phase 2: Propagation plan

After commissioning your investigations, return a structured wave plan for propagating those results through the rest of the graph. The wave plan is a sequence of **waves** executed programmatically after you return it.

### The `reassess_claims` operation

The key operation for this pipeline is `reassess_claims` (plural). It takes multiple claims and produces replacement claims informed by new evidence:

- `page_ids`: list of 8-char short IDs of claims to reassess together
- `in_light_of`: list of page IDs whose content should inform the reassessment. **If a page ID points to a question with an active judgement, the judgement is used instead.** This is the mechanism for incorporating subquestion investigation results — pass the investigated question's ID, and the system automatically resolves it to the latest judgement.
- `guidance`: free-text explaining what the reassessment should achieve (e.g. "reconcile these conflicting claims about market size in light of the subquestion findings on methodology")

### Typical patterns

**Incorporating investigation results:** After investigating a subquestion, use `reassess_claims` on the claims that should be updated in light of the findings. Pass the investigated question's ID to `in_light_of`.

**Reconciling conflicting claims:** If you investigated a subquestion to resolve tension between claims A and B, use `reassess_claims` with `page_ids: [A, B]`, `in_light_of: [subquestion_id]`. The operation will decide whether to merge, split, or individually update the claims.

**Updating dependent claims:** After claims are updated (by earlier waves), upstream claims that cite them may need updating too. Use `reassess_claims` on those upstream claims.

**Reassessing questions:** Use `reassess_question` (with `page_id`) to re-run the judgement for a question after its considerations have been updated.

**Substituting a superior source:** When the feedback says to use a specific page as the source of analysis for a subject (e.g. "use page `abcd1234` as the basis for X"), you need to propagate that substitution through the dependency chain:

1. Use `explore_page` to trace which claims currently rely on the inferior source, and which questions' judgements depend on those claims.
2. Use `reassess_claims` on the affected claims with the superior page in `in_light_of` and `guidance` explaining the substitution (e.g. "Rewrite this claim to draw on the analysis in `abcd1234` instead of `efgh5678`").
3. After claims are updated, use `reassess_question` on each question whose judgement depended on the updated claims. Pass the superior page and/or the updated claims in `in_light_of`. Remember that judgements can depend on other judgements — if a parent question's judgement cites a child question's judgement that was based on the inferior source, you need to `reassess_question` on the child first, then the parent in a later wave.
