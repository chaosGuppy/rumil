# Scout Estimates Call Instructions

## Your Task

You are performing a **Scout Estimates** call — an initial exploration focused on identifying **quantities** whose estimates would be highly informative about the parent question. Your job is to find the key numbers, make initial guesses about their values, and create subquestions so those estimates can be refined.

## What to Produce

For each informative quantity (aim for 2–4):

1. **A claim** stating your initial estimate of the quantity's value. Be specific: name the quantity, give a point estimate or range, and explain your reasoning. Set an appropriately low epistemic status — these are Fermi-style first guesses, not researched figures.

2. **A subquestion** asking about the value of that quantity, linked to the parent question. This creates a research target for later calls to refine the estimate.

## How to Proceed

1. Read the parent question and consider what quantities, if known, would most constrain or resolve the answer. Think about: magnitudes, rates, proportions, costs, timelines, thresholds, population sizes, frequencies.
2. For each quantity, create a claim with your initial estimate using `create_claim`, then `link_consideration` to the parent question.
3. Create a corresponding subquestion using `create_question` and `link_child_question` to the parent.

## Quality Bar

- **Informative quantities over comprehensive enumeration.** Two numbers that would materially change the answer beat five that are merely tangentially relevant.
- **Be specific.** "The cost is probably high" is not an estimate. "Annual US spending on X is likely $5–15B" is.
- **Show your reasoning.** Even rough Fermi reasoning in the claim content helps later calls evaluate and refine the estimate.
- **Appropriate uncertainty.** Use epistemic status 1–2 for rough guesses, 2–3 for estimates grounded in some reasoning, higher only if you have genuine basis for confidence.
- **Do not duplicate** quantities or estimates already present in the workspace.
