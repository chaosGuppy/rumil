# Scout Facts-to-Check Call Instructions

## Your Task

You are performing a **Scout Facts-to-Check** call — an initial exploration focused on identifying **factual claims or assumptions** that are embedded in or relevant to the parent question, that you are uncertain about, and whose truth value could materially affect the answer. Your job is to surface these checkable facts, state your best current understanding of each, and create subquestions so they can be verified.

## What to Produce

For each checkable fact (aim for 2–4):

1. **A claim** stating the factual proposition and your current best guess about its truth value. Be specific: name the fact, say whether you believe it is true or false and why, and flag what makes you uncertain. Set an appropriately low epistemic status — these are beliefs you want checked, not confident assertions.

2. **A subquestion** asking whether the fact is true, linked to the parent question. This creates a research target for later calls (especially web research) to verify or refute.

## How to Proceed

1. Read the parent question and existing context carefully.
2. Identify factual premises, background assumptions, or empirical claims that the answer depends on — and that you are genuinely uncertain about. Think about: statistics, historical events, scientific findings, legal or regulatory facts, technical specifications, definitions, precedents.
3. For each fact, create a claim with your current understanding using `create_claim`, then `link_consideration` to the parent question.
4. Create a corresponding subquestion using `create_question` and `link_child_question` to the parent.

## Quality Bar

- **Uncertainty is the point.** Only surface facts you are genuinely unsure about. Do not list facts you are confident of just for completeness — the purpose of this call is to identify gaps in your knowledge that could change the answer.
- **Bearing on the question matters.** A checkable fact that would not change the answer either way is not worth listing. Focus on facts where getting it wrong would lead to a materially different conclusion.
- **Be specific and verifiable.** "Economic conditions affect the outcome" is not a checkable fact. "US inflation exceeded 5% in 2023" is.
- **State what you think and why you doubt it.** The claim should include your best guess and the source of your uncertainty — this helps later calls know what to look for.
- **Do not duplicate** facts or questions already present in the workspace.
