# Scout Robustify

## Your Task

You are performing a **Scout Robustify** call — an exploration focused on suggesting variations of the scope claim that are more robustly true while remaining substantive.

## What to Produce

For each robust variation (aim for 1 or 2):

1. **A claim** describing the more robust version of the original claim. Use `CREATE_CLAIM` to create the variant, then use `LINK_VARIANT` to link it back to the original scope claim. This preserves the original claim while recording the relationship.

2. In the claim body, explain briefly what makes this variation more robust and why it still has enough substance to be useful.

## Robustification Strategies

Consider these approaches (use whichever are most appropriate for the claim at hand):

- **Lower bounds instead of point estimates.** If the original says "X is about 50%", a more robust version might be "X is at least 30%."
- **Conditional claims.** "If assumption A holds, then X" is more robust than bare "X" when A is uncertain but plausible.
- **Narrower scope.** "X holds in domain D (where evidence is strongest)" instead of "X holds everywhere."
- **Weaker quantifiers.** "Most Fs are G" instead of "All Fs are G."
- **Dropping the weakest conjunct.** If the original asserts A and B and C, and C is the weakest link, assert just A and B.

## How to Proceed

1. Read the scope claim and existing context carefully.
2. Identify what makes the claim fragile — is it overly precise? Too universal? Dependent on uncertain assumptions? Making a strong causal claim where only correlation is established?
3. For each fragility, suggest a variation that removes or reduces it.
4. After creating each variation, use `LINK_VARIANT` to link the new claim to the scope claim.
5. Set robustness scores higher than the original — that is the whole point.
6. Set credence at least as high as the original, and usually higher (a weaker claim should be more credible).

## Quality Bar

- **Still substantive.** "Something might happen" is not useful. The robust version must still say something specific enough to bear on research questions and decisions.
- **Genuinely more robust.** The variation should survive more scenarios, not just be vaguer. A well-supported lower bound beats a vague hedge.
- **Explain the trade-off.** Note what precision or scope is being sacrificed and why the trade-off is worthwhile.
- **Do not duplicate** variations already present in the workspace.
- **Set credence and robustness honestly.** The robust version should have higher higher credence than the original.
