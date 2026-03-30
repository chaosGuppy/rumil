# Scout Stress-Test Cases

## Your Task

You are performing a **Scout Stress-Test Cases** call — an exploration focused on identifying concrete scenarios that could serve as hard tests for the scope claim, especially boundary cases where the different stories predict different outcomes.

## What to Produce

For each stress-test case (aim for 2-4):

1. **A question** of the form "What does [scenario] tell us about [the claim]?" The question will be automatically linked to the scope claim.

2. For each candidate: briefly describe the scenario, explain why it would be a good test (what makes it hard for the claim to pass or fail), and note which stories it would help discriminate between.

Use `CREATE_QUESTION` to create each stress-test question.

## How to Proceed

1. Read the scope claim, existing how-true and how-false stories, and other context carefully.
2. Identify concrete, specific scenarios that would stress-test the claim — especially boundary cases, edge cases, or extreme conditions where competing stories diverge.
3. Frame each as a question about what the scenario tells us.
4. Don't perform the full analysis of each case — just identify promising cases for further investigation.

## Quality Bar

- **Concrete and specific.** "What about extreme cases?" is not useful. "What does the 2008 financial crisis tell us about [the claim]?" is specific.
- **Discriminating power.** The best stress tests are ones where the how-true and how-false stories predict noticeably different outcomes.
- **Hard tests.** Prioritize scenarios that are genuinely challenging for the claim, not easy confirmations.
- **Do not duplicate** scenarios already present in the workspace.
