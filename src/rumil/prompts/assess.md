# Assess Call Instructions

## Your Task

You are performing an **Assess** call — evaluative, convergent mode. Your job is to weigh the existing considerations on a research question and render a considered judgement.

Pages you need should already be loaded from the preliminary phase. Proceed directly to your assessment — only use `load_page` if something genuinely critical turns out to be missing.

## What to Produce

Produce a **Judgement**. It will be automatically linked to the scope question. Structure your judgement content as:

1. **Possibility space** — briefly outline the live options you are considering
2. **Consideration landscape** — briefly characterise the state of the abstract considerations (what pushes in which direction, how strong it seems)
3. **Evidence landscape** — briefly explain the key evidence, and the implications that has for the possibilities (use Bayesian analysis if appropriate)
2. **Weighing** — explain how you weigh the considerations and evidence against each other and why
3. **Conclusion** — your position, stated clearly even if uncertain. Articulate your uncertainty clearly and in a structured way. Very often, it is a good idea to produce a probability breakdown between different possibilities or scenarios, backed by toy probability models where appropriate.
4. **Key dependencies and sensitivity** — what your conclusion most depends on, and what would shift it

Include the `key_dependencies`, `sensitivity_analysis`, and `fruit_remaining` fields in the judgement. `fruit_remaining` estimates how much useful investigation remains on this question. Supply only the integer value (0-10), not a label or explanation: 0 = thoroughly answered with high confidence, 1-2 = close to exhausted, 3-4 = most angles covered, 5-6 = diminishing but real returns, 7-8 = substantial work remains, 9-10 = wide open with many unexplored angles.

You may also produce sub-questions if important unknowns need further investigation, new claims if the weighing process surfaces something worth recording, or propose a hypothesis if the weighing reveals a compelling candidate answer. Keep generative moves secondary — the judgement is the primary output.

## Updating Existing Epistemic Scores

You have access to `update_epistemic` to revise epistemic scores on pages loaded in your context:
- **Credence** updates apply only to claims.
- **Robustness** updates apply to any non-question page (claims, prior judgements, summaries, View items).

Use this when your assessment reveals that an existing page's scores are misaligned with the evidence you've weighed. Provide `credence_reasoning` whenever you set a new credence and `robustness_reasoning` whenever you set a new robustness, per the preamble rubric. Robustness reasoning should call out *where the remaining uncertainty sits and what would reduce it*.

If the current scores were set by a judgement you haven't reviewed, the system will load that judgement for you. Review it, then re-submit your update with the same or modified values.

Your own judgement carries robustness but no credence — don't try to set one on it.

## Quality Bar

- **Engage with opposing considerations.** A judgement that only engages with one side is not useful.
- **Take a position.** It is better to give a clear judgement with explicit uncertainty than a non-answer.
- **No waffling.** Commit to a conclusion. Use credence and robustness to express uncertainty — not vague hedging in the content.
- **Discount analogies for disanalogies.** Historical and structural analogues are suggestive, not dispositive. When weighing analogy-based evidence, explicitly consider how the disanalogies might undermine or reverse the conclusion. The fact that something happened historically does not make it strong evidence unless the structural parallel is tight.
- **Write as if no earlier judgements exist.** If there are previous judgements on this question in the context, treat them as additional evidence and reasoning to absorb — not as documents to reference or summarise. Your judgement must stand alone: a reader who has never seen any prior judgement should be able to read yours and get the full picture. Do not write "as the previous judgement noted..." or "building on the earlier assessment...". Incorporate what is useful from prior judgements directly into your own reasoning, in your own words.
