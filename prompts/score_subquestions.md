# Subquestion Scoring

You are evaluating subquestions (or a parent question) for a research workspace. Your job is to score each item on two dimensions:

- **Impact** (0–10): How much would answering this question help resolve the parent question? A score of 10 means it is critical and would substantially advance understanding. A score of 0 means it is tangential or redundant.

- **Fruit** (0–10): How much useful investigation can the system still apply to this question? A score of 10 means the question is wide open with many unexplored angles. A score of 0 means it has been thoroughly investigated or is unanswerable with available tools.

For each question, provide brief reasoning (1–2 sentences) explaining your scores. Focus on the marginal value of further investigation given what has already been discovered.

## Per-Type Fruit Scoring

When asked to score remaining fruit per call type, evaluate each type independently:

- **development**: How much value remains in deeper investigation of existing subquestions (find_considerations, web_research, recursion)? Consider how many subquestions exist, how thoroughly they have been investigated, and whether further development would meaningfully advance the parent question. High scores mean many subquestions are under-investigated; low scores mean existing subquestions are well-covered.
- **scout_subquestions**: How much value remains in identifying new subquestions? High if the question likely has important decompositions not yet surfaced; low if the question tree is well-decomposed.
- **scout_estimates**: How much value remains in identifying quantities whose estimates would be informative? High if there are clearly relevant quantities not yet identified.
- **scout_hypotheses**: How much value remains in surfacing hypotheses? High if the hypothesis space feels under-explored.
- **scout_analogies**: How much value remains in finding illuminating analogies? High if relevant analogies have not been surfaced.
- **scout_paradigm_cases**: How much value remains in identifying concrete paradigm cases? High if paradigm cases would help and none have been surfaced.
- **scout_factchecks**: How much value remains in identifying uncertain facts to check? High if there are likely important factual uncertainties not yet flagged.

Use the same 0–10 scale. Score each type based on the call history provided — if many rounds of a scout type have already run, its remaining fruit is likely lower. Score independently: scouting fruit and development fruit are separate dimensions.
