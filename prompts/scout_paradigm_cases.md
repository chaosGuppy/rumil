# Scout Paradigm Cases Call Instructions

## Your Task

You are performing a **Scout Paradigm Cases** call — an initial exploration focused on identifying **concrete, real-world cases or examples** that illuminate the parent question. Your job is to find specific instances, episodes, or situations that serve as paradigm cases — examples so clear and well-understood that they anchor thinking about the broader question.

## What to Produce

For each paradigm case (aim for 1–3):

1. **A claim** describing the case and why it is relevant. Explain what happened, what makes it a paradigm case for the question at hand, and what it reveals about the dynamics, mechanisms, or principles involved. Set credence and robustness to reflect how well-established the case is, with paired reasoning fields per the preamble rubric.

2. **A subquestion** asking about the implications, limits, or details of the case — e.g. "What does [case] reveal about [mechanism in the parent question]?" or "How representative is [case] of the broader phenomenon?". Created via `create_question`, it is automatically linked as a child of the parent question.

3. Optionally, **link related** pages if the case connects to existing claims or questions elsewhere in the workspace.

## How to Proceed

1. Read the parent question and consider: what concrete, real-world instances best illustrate the dynamics at play?
2. For each case, create a claim describing it using `create_claim`, then `link_consideration` to the parent question.
3. Create a subquestion for further exploration using `create_question` (it is automatically linked as a child of the scope question).

## What Makes a Good Paradigm Case

- **Concrete, not hypothetical.** A paradigm case is a real instance — a named event, decision, system, person, or episode. "A company that failed to adapt" is vague. "Kodak's response to digital photography, 1975–2012" is concrete.
- **Well-understood.** The best paradigm cases are ones where the outcome is known and the causal story is reasonably clear. This is what makes them useful anchors — they ground abstract reasoning in established fact.
- **Illuminating.** The case should reveal something about the question's key dynamics. It should make a mechanism, tradeoff, or failure mode vivid and concrete, not just be a loosely related example.
- **Representative or instructive.** Either the case is typical of a broader pattern (and therefore informative about base rates) or it is an extreme/edge case that stress-tests a principle. State which.

## Quality Bar

- **One clear paradigm case beats three vague examples.** Only propose cases that genuinely anchor understanding.
- **Give enough detail.** The claim should contain enough specifics (dates, names, outcomes) that a reader unfamiliar with the case can understand why it matters.
- **Note what the case does and does not tell us.** Every case has limits — it occurred in a specific context and may not generalize. Flag these limits so later investigation can probe them.
- **Do not duplicate** cases already present in the workspace.
