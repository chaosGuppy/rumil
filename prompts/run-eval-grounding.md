# Run Evaluation: Grounding & Factual Correctness

The question: when this run's claims could have been grounded in a source, were they? And when they were grounded, is the citation honest?

Thin grounding isn't automatically a failure — some claims are structural or analytical and don't call for an external source. But a factual claim that *could* be grounded and isn't, or one that cites a source that doesn't actually support it, is a real defect. Focus your attention on load-bearing claims; a weak citation on a throwaway claim matters less than a weak citation on a claim the top judgement rests on.

## What to look for

1. **Source backing** — for claims that should be externally grounded, are they? Do the cited sources actually support what they're cited for, or does the citation break on inspection?
2. **Factual accuracy** — are specific factual assertions correct? Use WebSearch to verify load-bearing ones when you can.
3. **Evidence specificity** — vague appeals ("studies show", "experts agree") that never pin down what they rest on. These read as grounded but aren't.
4. **Misrepresentation** — sources cited in a way that distorts what they actually say, via cherry-picking, context-stripping, or paraphrase drift.

## How to work

Find `[ADDED BY THIS RUN]` claims with `explore_subgraph`. For load-bearing factual claims, open the cited sources and verify the citation holds. For high-stakes external facts, verify via WebSearch.

## Output

- **Summary** — 2–3 sentences on grounding quality overall.
- **Strengths** — what the run did well (solid sourcing on the things that mattered).
- **Weaknesses** — poor grounding, missing sources, factual errors — each with page IDs.
- **Verified claims** — claims you checked and found sound.
- **Problematic claims** — unsupported, incorrect, or misleadingly cited.
- **Overall** — one paragraph.
