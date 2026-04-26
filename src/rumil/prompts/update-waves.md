## Propagation plan

After making your direct edits, return a structured **execution plan** for propagating those changes through the rest of the graph. This plan is a sequence of **waves** that will be executed programmatically after you return it.

Each wave contains one or more operations. Operations within the same wave execute **concurrently**. Waves execute **in sequence** — all operations in wave 1 complete before wave 2 begins.

There are three types of propagation operations:

- **reassess_claim**: Reassess a single upstream claim that cites pages you have already updated (either directly, or via earlier waves). This creates a new version of the claim with updated content, credence, and robustness. Provide `page_id` and optionally `findings_summary`.
- **reassess_claims** (plural): Reassess multiple claims together — typically to reconcile conflicting or related claims in light of new evidence. Provide `page_ids` (list of 8-char short IDs), `in_light_of` (list of page IDs whose content should inform the reassessment — if a page ID points to a question with an active judgement, the judgement is used instead), and `guidance` (free-text explaining what the reassessment should achieve, e.g. "reconcile these conflicting claims about X in light of the subquestion findings"). This operation can merge, split, update, or replace claims, and can add or remove consideration links.
- **reassess_question**: Reassess a question's judgement by running a full assessment call. This re-evaluates the question in light of all its current considerations — including any claims updated directly or in earlier waves.

## Graph structure and ordering

Pages in the workspace cite each other. Claims and judgements typically cite other claims and judgements — not questions directly. A question's judgement summarises the considerations (claims) bearing on that question, so updating claims that feed into a question should happen **before** reassessing that question's judgement.

The typical dependency chain looks like:

1. Leaf claims (directly affected) — **handled in your direct edits**
2. Questions whose considerations include those claims → `reassess_question` produces a new judgement
3. Claims that cite the judgements updated in step 2
4. Questions whose considerations include those claims → and so on up the graph

Because each operation reads the current state of linked pages, **order matters**: update dependencies in earlier waves so that later waves see the updated content.

Within a wave, independent operations run concurrently. Use this for claims that don't depend on each other, or questions that don't share updated considerations.

## Budget

You have a budget of **{budget} propagation operations** total (across all waves). Focus on the updates that will have the most impact on the quality of the final judgement on the target question. Not every path through the graph needs updating — prioritise the most important chains.

## Tips

- A change that affects a high-credence claim cited by the target question's judgement is high-impact.
- A change that slightly refines a peripheral claim is low-impact.
- If several claims feed into the same question, you can update them concurrently (same wave) and put the question reassessment in the next wave.
- If a claim cites a judgement that you plan to update, reassess the question (to update the judgement) before reassessing that claim.
- You don't need to reassess every intermediate question — only the ones where updated considerations meaningfully change the picture.
- When in doubt about a page's role or what it cites, use `explore_page` to inspect it and its connections.
