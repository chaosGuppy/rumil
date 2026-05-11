Please judge this pair of essay continuations.

You'll see four inline artifacts in your first user message:
- `prefix` — the shared essay opening both continuations build on
- `essay_a` — continuation A
- `essay_b` — continuation B
- `rubric` — the dimension you're judging on

When you have your judgement ready (having reached high confidence or used most of your budget) call `finalize` with:
- `reasoning`: your concrete evidence-grounded reading of both continuations against the rubric. Cite specifics from each. Don't hedge.
- `verdict`: one of the seven canonical preference labels. The schema constrains the choice; pick the one that best fits your reasoning.

Judge blindly on rubric quality alone. The display labels A / B are arbitrary and assigned deterministically by the harness.
