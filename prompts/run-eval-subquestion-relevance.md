# Run Evaluation: Subquestion Relevance

A subquestion earns its place when answering it would materially change the answer to the parent. Everything else is noise — tangential curiosity, restated parents, or decomposition-for-decomposition's-sake.

Think of the ideal subquestion set as a Bayesian network: each child should capture a genuine source of uncertainty that's conditionally informative about the parent *given* answers to its siblings. If a subquestion's answer wouldn't move the needle much once the others are answered, it's not pulling its weight.

## What to look for

1. **Informativeness** — does answering each subquestion actually advance understanding of the parent? Or is it tangential?
2. **Coverage** — are the big angles represented? Is there something a thoughtful analyst would obviously ask that's missing?
3. **Redundancy** — are multiple subquestions asking the same thing in different words? Overlap means wasted effort.
4. **Granularity** — too broad (restates the parent) or too narrow (splits hairs that don't matter)?
5. **Strategic value** — do the subquestions target high-leverage uncertainties (resolving them would substantially shift the parent's answer) or easy-to-answer low-impact details?

## How to work

Map the question hierarchy this run created. For each parent-child link, ask: *would a good answer here actually change the parent's answer?* Then scan siblings for overlap and obvious gaps.

## Output

- **Summary** — 2–3 sentences.
- **Strengths** — what the run decomposed well.
- **Weaknesses** — specific uninformative, redundant, or mis-granular subquestions (page IDs).
- **Coverage gaps** — important angles that aren't asked.
- **Redundancy** — groups of overlapping subquestions.
- **Overall** — one paragraph on whether the decomposition is well-aimed.
