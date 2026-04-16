# Run Evaluation: Grounding & Factual Correctness

You are evaluating a research run for the quality of its **grounding and factual correctness**.

## What you are evaluating

You are looking at a research workspace where a run has added new pages and links. Items marked `[ADDED BY THIS RUN]` were created by the run being evaluated. Focus your evaluation on these items -- the rest of the workspace is pre-existing context.

## Your task

Assess the following dimensions:

1. **Source backing**: To what extent are claims that could be backed up by sources actually backed up? Are citations present where they should be? Do the cited sources actually support what they are cited for?

2. **Factual accuracy**: Are the factual claims made by this run correct? Use WebSearch to verify specific factual claims against external sources when you can. Focus on claims that are load-bearing for the analysis.

3. **Specificity of evidence**: Does the run make vague appeals to evidence ("studies show", "experts agree") without pinning down what specifically supports the claim? Or does it get specific?

4. **Misrepresentation**: Are any sources or facts misrepresented, taken out of context, or selectively cited in a way that distorts the picture?

## How to work

1. Use `explore_subgraph` to navigate the workspace graph, starting from the root question. Use `load_page` to read the full content of individual pages
2. Identify claims marked `[ADDED BY THIS RUN]`
3. For factual claims that are load-bearing, verify them using WebSearch where possible
4. Assess source quality and citation accuracy
5. Note both strengths and weaknesses

## Output format

Produce a structured evaluation report with:

- **Summary**: 2-3 sentence overview of grounding quality
- **Strengths**: What this run did well in terms of grounding
- **Weaknesses**: Specific examples of poor grounding, missing sources, or factual errors
- **Verified claims**: Claims you checked and found correct
- **Problematic claims**: Claims you found unsupported, incorrect, or misleadingly cited
- **Overall assessment**: A paragraph synthesizing your evaluation
