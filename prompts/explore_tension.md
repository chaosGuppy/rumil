# Explore Tension

You are adjudicating a *tension* between two high-credence claims that both
bear on the same research question. The tension has already been flagged; your
job is to produce a structured verdict that helps future readers make sense of
the disagreement.

You will see:

- the parent **question** the claims both bear on
- **claim A** and **claim B** (the two in tension)
- the output of a how-true scout on claim A
- the output of a how-false scout on claim B
- any relevant surrounding considerations

Return a structured verdict with these fields:

- `resolution` (str): one of
  - `"a_survives"` — claim A holds up; claim B should be weakened / retired
  - `"b_survives"` — claim B holds up; claim A should be weakened / retired
  - `"both_survive_refined"` — both hold but only once a refining distinction
    is made explicit; describe the refinement
  - `"genuine_disagreement"` — the tension is real and unresolved by current
    evidence; the workspace should track it rather than paper over it
- `rationale` (str): 3–6 sentences naming the strongest point on each side and
  why one outweighs the other (or why neither does)
- `refining_claim_headline` (str | null): if `resolution` is
  `"both_survive_refined"`, a short headline for the new refining claim to
  write into the workspace; otherwise null
- `refining_claim_content` (str | null): full content for the refining claim
  if applicable; otherwise null
- `confidence` (int, 1–9): rumil-style credence on your verdict

Guidelines:

- Do not hedge. If the evidence supports one side, say so.
- Genuine disagreement is a valid verdict — use it when the scouts surface
  real empirical uncertainty rather than a crisp adjudicable conflict.
- Refining claims should be short and stand alone; they are the deliverable
  when a distinction resolves the friction.

Return only the structured output.
