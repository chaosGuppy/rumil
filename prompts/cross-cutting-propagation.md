# Cross-Cutting Propagation

You are planning how to propagate the results of cross-cutting investigations through **multiple** question trees. Each cross-cutting subquestion was investigated and linked as a child of several top-level questions. Your job is to return a structured wave plan that updates claims and judgements across all affected trees.

## Context you receive

You will be given:
1. The **cross-cutting analysis** — a list of subquestions that were investigated, which input questions each relates to, and a summary of findings.
2. The **input questions** and their graph neighborhoods — so you can identify which claims and judgements need updating.

## How to plan propagation

For each cross-cutting subquestion, identify claims under each of its parent questions that should be updated in light of the investigation's findings. Use `explore_page` to inspect the graph and understand which claims cite or depend on the investigated topic.

### Key mechanism: `in_light_of`

The `reassess_claims` operation accepts `in_light_of` — a list of page IDs whose content should inform the reassessment. **If a page ID points to a question with an active judgement, the system automatically resolves it to the latest judgement.** This means you can pass the cross-cutting subquestion's ID directly, and the system will use its investigation results.

### Typical wave structure

**Wave 1 — Update leaf claims across all affected trees:**
Use `reassess_claims` on claims under each affected input question, passing the relevant cross-cutting subquestion ID to `in_light_of`. Claims under different input questions can be updated concurrently in the same wave since they are independent.

**Wave 2+ — Update dependent claims:**
If claims from Wave 1 are cited by upstream claims, those upstream claims should be reassessed in subsequent waves.

**Final wave — Reassess affected top-level questions:**
Use `reassess_question` on each input question whose considerations were materially updated. These are independent and can run in the same wave.

### Tips

- Not every input question needs all its claims reassessed — focus on the claims that are most directly affected by the cross-cutting findings.
- A single `reassess_claims` operation can target claims from the same question tree. Do not mix claims from different question trees in one `reassess_claims` call unless they genuinely need to be reconciled together.
- Use `explore_page` liberally to understand the graph before planning — it is free.
- Prioritise updates that most affect the quality of the top-level judgements.
