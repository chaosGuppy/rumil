# Subquestion Scoring

You're scoring subquestions **in batches**. The first message gives you the parent question (headline, abstract, latest judgement when available). Each subsequent message presents a batch of subquestions with abstracts and latest assessments. Score every item — no skipping.

Three dimensions per item:

- **impact_on_question** (0–10) — how much would answering this subquestion help resolve the parent? 0 = irrelevant, 1–3 = tangential or redundant (marginal value at best), 4–6 = matters but won't be decisive, 7–9 = central, 10 = the parent hinges on it.

- **broader_impact** (0–10) — how strategically important is this subquestion in general? Would its answer shift major-outcome probabilities or be action-relevant? 0 = irrelevant outside this question, 1–3 = narrow relevance, 4–6 = important in its subdomain, 7–9 = matters across many strategic questions, 10 = one of a small handful of the most critical questions for understanding the strategic picture.

- **fruit** (0–10) — how much useful investigation can still be applied? 0 = thoroughly investigated or unanswerable, 1–2 = close to exhausted, 3–4 = most angles covered, 5–6 = diminishing but real returns, 7–8 = substantial work remains, 9–10 = wide open. If a `fruit_remaining` estimate from an existing assessment is visible, default to it but revise based on broader context.

## Reading the latest judgement

Each subquestion may show one or more **active judgements**, tagged with credence and robustness:

- **Credence (1–9)** — the system's degree of belief in the judgement's headline answer (1 = very unlikely, 5 = uncertain, 9 = very likely). What the workspace currently believes.
- **Robustness (1–5)** — how well-supported that belief is: how much scrutiny the answer has survived, how rich the evidence base is, how stable the credence would be under further investigation. 1 = tentative first pass, 5 = thoroughly vetted and unlikely to move.

**Robustness is the single most important signal for `fruit`.** Low-robustness judgement (1–2/5) almost always means substantial room for improvement — more evidence, more scrutiny, or an alternative framing could meaningfully shift either the answer or confidence in it. High-robustness judgement (4–5/5) is mostly exhausted; further investigation unlikely to change much. Treat low robustness as a strong reason to score `fruit` higher, high robustness as a strong reason to score it lower — even when the current answer looks plausible.

Brief reasoning (1–2 sentences) per item. The frame: **marginal value of further investigation given what's already been discovered** — not how interesting the subquestion is in the abstract.
