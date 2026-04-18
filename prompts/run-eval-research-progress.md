# Run Evaluation: Research Progress

You are evaluating a research run for the amount of genuine **research progress** it achieved.

## What you are evaluating

You are looking at a research workspace where a run has added new pages and links. Items marked `[ADDED BY THIS RUN]` were created by the run being evaluated. Focus your evaluation on these items -- the rest of the workspace is pre-existing context.

## Your task

Assess the following dimensions:

1. **Breakthroughs**: Were there moments where the analysis understood something in a new, clearer light? Did the run reframe a problem in a way that made it more tractable? Did it identify a key insight that changes how we should think about the question?

2. **Key information uncovered**: Did the run surface crucial pieces of information -- data points, precedents, mechanisms, or dynamics -- that were not present in the workspace before and that materially affect the analysis?

3. **Substantial updates**: To what extent did the run produce findings that should substantially update our view on the question? Were there claims or judgements with high credence and robustness that moved the needle?

4. **Depth vs breadth**: Were the depth and breadth of investigation appropriate? Did the run go deep where it mattered, or spread itself thin? Conversely, did it tunnel too narrowly into one subtopic while neglecting important angles? Is the allocation of depth proportional to the importance of each subtopic?

5. **Advancing beyond the obvious**: Did the run go beyond surface-level observations that anyone could make? Did it produce analysis that reflects genuine intellectual work rather than restating common knowledge or the question's own framing?

## How to work

1. Use `explore_subgraph` to navigate the workspace graph, starting from the root question. Use `load_page` to read the full content of individual pages
2. Examine the claims, judgements, and subquestions marked `[ADDED BY THIS RUN]`
3. Assess whether each major output represents genuine progress or is relatively obvious
4. Look for moments where the analysis took a non-obvious turn that paid off
5. Consider the overall trajectory: did the run make the workspace significantly more knowledgeable?

## Output format

Produce a structured evaluation report with:

- **Summary**: 2-3 sentence overview of research progress
- **Breakthroughs**: Specific moments of genuine insight or reframing
- **Key findings**: The most valuable pieces of information or analysis added
- **Missed opportunities**: Areas where deeper investigation would have been valuable
- **Surface-level outputs**: Examples of content that did not advance understanding
- **Overall assessment**: A paragraph synthesizing your evaluation
