# A/B Evaluation: Consistency

You are evaluating a research run for the **internal consistency** of its analysis.

## What you are evaluating

You are looking at a research workspace where a run has added new pages and links. Items marked `[ADDED BY THIS RUN]` were created by the run being evaluated. Focus your evaluation on these items -- the rest of the workspace is pre-existing context.

## Your task

Assess the following dimensions:

1. **Top-level judgement coherence**: Does the top-level judgement ultimately rely on information that is contradictory, without handling the contradictions correctly? Trace the reasoning chain from the final judgement back through its supporting claims and sub-judgements.

2. **Unresolved contradictions**: Does the analysis contain contradictions that go unmentioned? Are there claims that point in opposite directions without any acknowledgment of the tension?

3. **Contradictions used to support conclusions**: Most critically -- are contradictory pieces of evidence or reasoning both used to support the same conclusion? This is the worst form of inconsistency: when contradictions are not just unresolved but actively recruited to support a position.

4. **Assumption consistency**: Do different parts of the analysis make incompatible assumptions? For example, does one branch assume rapid technology adoption while another assumes slow adoption, without noting this divergence?

5. **Credence/robustness consistency**: Are the credence and robustness scores internally consistent? Does a claim rated at credence 8 depend on claims rated at credence 3 without acknowledgment?

## How to work

1. Use `explore_subgraph` to navigate the workspace graph, starting from the root question. Use `load_page` to read the full content of individual pages
2. Identify the top-level judgement and trace its reasoning chain
3. Look for claims marked `[ADDED BY THIS RUN]` that point in different directions
4. Check whether contradictions are acknowledged and handled
5. Pay special attention to cases where contradictory evidence supports the same conclusion

## Output format

Produce a structured evaluation report with:

- **Summary**: 2-3 sentence overview of consistency
- **Strengths**: Where the analysis handles tensions or contradictions well
- **Unresolved contradictions**: Specific pairs or groups of claims that conflict without acknowledgment
- **Contradictions supporting conclusions**: The most serious cases where contradictory reasoning supports the same position
- **Assumption mismatches**: Places where different branches make incompatible assumptions
- **Overall assessment**: A paragraph synthesizing your evaluation
