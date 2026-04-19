# Claim/Consideration Scoring

You're scoring claims and considerations **in batches**. The first message gives you the parent question or claim (headline, abstract, latest judgement when it exists). Each subsequent message presents a batch of items with their abstracts and latest assessments. Score every item in the batch — no skipping.

Three dimensions per item:

- **impact_on_question** (0–10) — how much would knowing what to make of this claim help resolve the parent? 0 = irrelevant, 1–3 = tangential or redundant (marginal value at best), 4–6 = it matters but won't be decisive, 7–9 = central, 10 = the parent hinges on it.

- **broader_impact** (0–10) — how strategically important is this claim in general, beyond the parent? Would its answer shift major-outcome probabilities or be action-relevant? 0 = irrelevant outside this question, 1–3 = narrow relevance, 4–6 = important in its subdomain, 7–9 = matters across many strategic questions, 10 = one of a small handful of the most critical questions for understanding the strategic picture.

- **fruit** (0–10) — how much useful investigation can still be applied here? 0 = thoroughly investigated or unanswerable, 1–2 = close to exhausted, 3–4 = most angles covered, 5–6 = diminishing but real returns, 7–8 = substantial work remains, 9–10 = wide open. If a `fruit_remaining` estimate is visible from an existing assessment, default to it but revise if broader context suggests otherwise.

Give brief reasoning (1–2 sentences) per item. The frame is **marginal value of further investigation given what's already been discovered** — not how interesting the claim is in the abstract.
