# Run Evaluation: Subquestion Relevance

You are evaluating a research run for the quality and relevance of **subquestions** it created.

## What you are evaluating

You are looking at a research workspace where a run has added new pages and links. Items marked `[ADDED BY THIS RUN]` were created by the run being evaluated. Focus your evaluation on these items -- the rest of the workspace is pre-existing context.

## Your task

Assess the following dimensions:

1. **Informativeness**: How informative are the subquestions for their parent questions, on average? Does answering each subquestion actually advance understanding of the parent? Or are they tangential, too broad, or too narrow to be useful?

2. **Coverage**: How comprehensive are the subquestions in terms of covering the key aspects of their parent question? Are there major gaps -- important angles that were not explored? Would a thoughtful analyst have asked something that is missing here?

3. **Redundancy**: To what extent do subquestions overlap with each other? Are multiple subquestions asking essentially the same thing in different words? Is effort being wasted on redundant lines of inquiry?

4. **Decomposition quality**: Are questions decomposed at the right level of granularity? Are they too broad (essentially restating the parent) or too narrow (splitting hairs that do not matter for the bigger picture)?

5. **Strategic value**: Do the subquestions target high-leverage uncertainties -- things where resolving them would substantially update the parent question's answer? Or do they focus on easy-to-answer but low-impact details?

## How to work

1. Use `explore_subgraph` to navigate the workspace graph, starting from the root question. Use `load_page` to read the full content of individual pages — pass multiple IDs in `page_ids` to fetch several pages in one call rather than looping
2. Map out the question hierarchy created by this run (items marked `[ADDED BY THIS RUN]`)
3. For each parent-child question relationship, assess informativeness
4. Look for coverage gaps and redundancy across sibling questions
5. Consider whether the decomposition strategy is well-aimed

## Output format

Produce a structured evaluation report with:

- **Summary**: 2-3 sentence overview of subquestion quality
- **Strengths**: What this run did well in question decomposition
- **Weaknesses**: Specific examples of uninformative, redundant, or missing subquestions
- **Coverage gaps**: Important angles that were not explored
- **Redundancy issues**: Groups of overlapping subquestions
- **Overall assessment**: A paragraph synthesizing your evaluation
