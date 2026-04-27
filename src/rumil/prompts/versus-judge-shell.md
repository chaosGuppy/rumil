# Essay-Continuation Pairwise Judgment

You are judging two continuations — **Continuation A** and **Continuation B** — of the same essay opening. Your job is to produce a direct comparison on one dimension and end with an explicit preference rating. Judge purely on the dimension below; the source of each continuation is not relevant and is not disclosed to you.

## What you are looking at

{location_desc}
{tool_section}
## Preference scale

End your response with exactly one of these ratings on its own line. **Match the label wording exactly** so it can be parsed.

- **A strongly preferred**: Continuation A is clearly and substantially better on this dimension
- **A somewhat preferred**: Continuation A is meaningfully better, though B has some merits
- **A slightly preferred**: Continuation A has a slight edge, but the difference is small
- **Approximately indifferent between A and B**: Both continuations are roughly equal on this dimension
- **B slightly preferred**: Continuation B has a slight edge, but the difference is small
- **B somewhat preferred**: Continuation B is meaningfully better, though A has some merits
- **B strongly preferred**: Continuation B is clearly and substantially better on this dimension

## The judgment dimension

The specific dimension you are judging on is below.

---

{task_body}

---

## Output format

Produce a structured comparison. Use whatever structure the dimension prompt above asks for, but the comparison must:

- Be grounded in concrete observations from the two continuations (quote specific passages{output_extras}).
- Explicitly contrast the two rather than describing each in isolation.
- End with the preference rating on its own line, using one of the exact labels above.

## Convergence

Don't stop until your sense of the difference between A and B has converged. But also don't over-explore — for short essay continuations, one careful pass through each text{convergence_extras} is usually enough.
