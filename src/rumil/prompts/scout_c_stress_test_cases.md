## the task

you're doing a **scout stress-test cases** call — identifying
concrete scenarios that could serve as **hard tests** for the scope
claim, especially boundary cases where the different stories
predict different outcomes.

## a few moves

before producing stress-test cases, name the cached take. what are
the obvious "let's see if this holds in X scenario" cases a sharp
person would reach for? write them down. for each, ask: does this
scenario actually **discriminate** between the how-true and
how-false stories, or would both predict the same outcome?
non-discriminating cases don't earn their place.

attack each candidate by asking: is this a *hard* test, or an easy
confirmation? cases where the claim obviously passes don't stress
it — the load-bearing cases are boundaries, edges, and conditions
where competing stories diverge.

## what to produce

for each stress-test case (aim for **2-4**):

1. **a question** of the form "what does [scenario] tell us about
   [the claim]?". use `create_question` — it auto-links to the
   scope claim.

2. for each: briefly describe the scenario, explain why it would
   be a good test (what makes it hard for the claim to pass or
   fail), and note which stories it would help discriminate between.

## how to proceed

1. read the scope claim, existing how-true and how-false stories,
   and other context carefully.
2. identify concrete, specific scenarios that would stress-test the
   claim — especially boundary cases, edge cases, or extreme
   conditions where competing stories diverge.
3. frame each as a question about what the scenario tells us.
4. don't perform the full analysis of each case — just identify
   promising cases for further investigation.

## quality bar

- **concrete and specific.** "what about extreme cases?" is not
  useful. "what does the 2008 financial crisis tell us about [the
  claim]?" is specific.
- **discriminating power.** the best stress tests are ones where
  the how-true and how-false stories predict noticeably different
  outcomes.
- **hard tests.** prioritise scenarios that are genuinely
  challenging for the claim, not easy confirmations.
- **don't duplicate** scenarios already in the workspace.
