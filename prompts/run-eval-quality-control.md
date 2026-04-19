# Run Evaluation: Quality Control

A precision sweep for concrete, glaring errors — the kind of thing that looks fine on a skim but is plainly wrong on close inspection. Not strategy, not tone, not coverage. Errors a careful reviewer would flag as broken or indefensible.

**Prioritise precision over recall.** A handful of sharp, defensible findings beats a long list of nitpicks. If a finding feels marginal or subjective, leave it out. Never pad the list.

## What to flag

In rough priority order:

1. **`broken_citation`** — a claim cites a source that doesn't support (or contradicts) what it asserts. Load both before flagging.
2. **`overconfident_claim`** — self-credence ≥ 7 with no supporting sources, or sources too thin to justify the score.
3. **`factual_error`** — a demonstrably wrong date, number, name, or causal relationship. Use WebSearch to verify when available.
4. **`intra_run_contradiction`** — two pages this run created that make incompatible claims without acknowledgement.
5. **`orphan_view_item`** — a view_item with no underlying claim or source it rests on.
6. **`other`** — any other concrete error. Still a matter of fact, not taste.

**Do not flag:**
- Stylistic quibbles or headline-phrasing preferences.
- Missing-but-plausible research directions (that's coverage, not error).
- Calibration drift on otherwise-defensible claims (Calibration handles it).
- General-quality impressions (General Quality handles them).

## How to work

Find `[ADDED BY THIS RUN]` pages with `explore_subgraph`. Read claims and their cited sources in full before flagging a citation issue — batch IDs in one `load_page` call.

Cap yourself at about **10 findings**. Hard max: 20. If you're at 10 and tempted to add another, check whether the candidate is sharper than your weakest existing finding. If yes, swap; if no, stop.

For each finding, cite exact page IDs and state the problem in one sentence.

An empty list is a legitimate and useful result. If you have no confident findings, say so.

## Output

Short narrative summary, then a **fenced JSON block** with the structured findings. The JSON is the machine-readable surface — tooling parses it to emit reputation events and render the dashboard. Keep it clean.

### Summary (1–3 sentences)

How many findings, how severe overall, any recurring category.

### Findings (JSON)

```json
{
  "findings": [
    {
      "kind": "broken_citation",
      "page_ids": ["c-ab12cd34"],
      "severity": "moderate",
      "evidence": "Claim c-ab12cd34 asserts X, but cited source s-ef56gh78 does not mention X.",
      "suggested_fix": "Remove the citation or restate the claim to match what the source says."
    }
  ]
}
```

`severity` values:

- `critical` — the run is actively misleading (broken citation on a load-bearing claim, factual error that will propagate).
- `moderate` — wrong in a way a reader notices on second inspection.
- `low` — minor but concrete defect (e.g. orphan view item with an easy fix).

If no findings, emit `{"findings": []}`. Do not invent findings to pad.
