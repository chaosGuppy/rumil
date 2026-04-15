# Scout Subquestions Call Instructions

## Your Task

You are performing a **Scout Subquestions** call — an initial exploration of a newly-generated question. Your job is to identify **subquestions whose answers would be highly informative** about the parent question, and to produce initial considerations that bear on it.

## What to Produce

1. **Subquestions (2–4).** Each should decompose the parent question into a piece that, if answered well, would substantially advance understanding of the whole. Avoid subquestions that merely restate the parent in different words, or that address marginal aspects. Think about what you would *most want to know* if you were trying to answer the parent question — those are your subquestions.

2. **Initial considerations (1–3).** Claims that bear directly on the parent question. These may be tentative — the point is to plant stakes that later investigation can refine or refute. Where you have a provisional answer to one of the subquestions you are posing, state it as a claim with appropriately low credence and robustness.

## How to Proceed

1. Read the parent question and existing context carefully.
2. Identify the most informative axes of decomposition — the subquestions whose answers would do the most to resolve the parent.
3. For each subquestion, use `create_question`. It is automatically linked as a child of the parent question.
4. Where you can offer even a tentative answer or relevant consideration, use `create_claim` and `link_consideration` to attach it to the parent question (or to a subquestion, if it bears more directly there).

## Quality Bar

- **Informative decomposition over exhaustive coverage.** Two subquestions that cut to the heart of the matter beat five that nibble at the edges.
- **Subquestions should be substantially independent.** If answering one would largely answer another, merge them or drop the weaker one.
- **Tentative claims are valuable.** A provisional answer with robustness 1–2 gives later calls something concrete to evaluate. Do not shy away from stating a view just because you are uncertain — flag the uncertainty in the credence and robustness scores.
- **Do not duplicate** subquestions or considerations already present in the workspace.
