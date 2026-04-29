# Scout Strengthen

## Your Task

You are performing a **Scout Strengthen** call — the scope claim already has high credence (it is believed to be robustly true). Your job is to suggest variations that are more precise, specific, or stronger while maintaining that high credence.

This is the opposite of robustification: instead of weakening for safety, you are tightening for informativeness.

## What to Produce

For each strengthened variation (aim for 1 or 2):

1. **A claim** describing the stronger version. Use `CREATE_CLAIM` to create the variant, then use `LINK_VARIANT` to link it back to the original scope claim.

2. In the claim body, explain what was strengthened and why credence should remain high.

## Strengthening Strategies

Consider these approaches (use whichever are most appropriate):

- **Add quantitative bounds.** If the original says "X increases Y", try "X increases Y by at least Z%."
- **Narrow error bars.** If the original says "at least 30%", can the evidence support "at least 40%"?
- **Strengthen quantifiers.** If "most Fs are G" and the evidence is strong, can it be "nearly all Fs are G"?
- **Add specificity.** If the original says "in domain D", can you name specific sub-domains or conditions where it holds even more strongly?
- **Remove unnecessary hedges.** If a conditional ("if A, then X") has A well-established, state X directly.
- **Add a conjunct.** If the original asserts A and B, and C is also well-supported, assert A and B and C.

## How to Proceed

1. Read the scope claim and existing context carefully — especially the how-false stories and assessment results.
2. Identify where the claim is weaker than it needs to be given the evidence.
3. For each weakness, suggest a variation that strengthens the claim.
4. After creating each variation, use `LINK_VARIANT` to link the new claim to the scope claim.
5. Set credence at least as high as the original — if you cannot maintain high credence, the strengthening is too aggressive.
6. Set robustness at least as high as the original.
7. Pair every score with its reasoning field per the preamble rubric — in credence_reasoning, explain why the stronger wording is still supported; in robustness_reasoning, say what would further firm it up or what could still move it.

## Quality Bar

- **Maintain credence.** The whole point is that the strengthened version should still be highly credible. If strengthening drops credence below 8, you have gone too far.
- **Genuinely stronger.** The variation should say more, not just rephrase the same thing. More precision, more specificity, tighter bounds.
- **Evidence-based.** Only strengthen where the existing research and context support it. Do not speculate.
- **Do not duplicate** variations already present in the workspace.
