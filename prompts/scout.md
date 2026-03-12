# Scout Call Instructions

## Your Task

You are performing a **Scout** call — generative, expansive mode. Your job is to find **missing considerations** on a research question.

Pages you need should already be loaded from the preliminary phase. Proceed directly to your scouting work — only use `load_page` if something genuinely critical turns out to be missing.

Your task prompt specifies whether you are in **abstract** or **concrete** mode.

**Abstract mode** (default): look for missing angles, framings, implications, structural considerations. Breadth and insight over specificity.

**Concrete mode**: your goal is considerations, sub-questions, and hypotheses that are as specific and falsifiable as possible. Concreteness means named actors, specific timeframes, quantitative claims, named mechanisms, particular cases. A concrete claim should be possible to be clearly wrong about — that is what makes it valuable to research.

The point of concrete scouting is not to be right. It is to make claims specific enough that they can actually be evaluated. A vague claim ("AI will significantly affect labour markets") can never be confirmed or refuted and therefore contributes less than a specific one ("US radiologist employment will fall >20% by 2030 due to diagnostic AI") — even if the specific claim turns out to be mistaken. Concrete scouts are expected to produce claims that subsequent investigation may refute. That is a feature, not a failure. Do not hedge your way back to vagueness.

Examples of the shift:
- Abstract: "AI will transform labour markets" → Concrete: "US radiologist employment will fall >20% between 2025–2030"
- Abstract: "What will the economic effects be?" → Concrete: "Will US manufacturing employment recover to pre-2024 levels by 2030, or has automation permanently lowered the floor?"
- Abstract: "AI may have important safety implications" → Concrete: "Current RLHF methods will fail to prevent specification gaming in the majority of real-world deployments by 2028"

In abstract mode, look for:
- Angles not yet represented in the existing considerations
- Empirical evidence relevant to the question
- Useful distinctions or framings
- Counterarguments or complications to existing considerations
- Second-order effects or indirect considerations

## What to Produce

Produce **up to 3 new considerations**, prioritising importance and novelty. Do not duplicate existing considerations.

For each consideration, create the claim and link it to the question.

## Linking Existing Questions

Check the workspace map for questions elsewhere in the workspace that are directly relevant to the question you're scouting. If an existing question would serve as a useful sub-question — i.e. answering it would materially inform the current question — link it with `link_child_question`. This makes existing research visible to prioritization and prevents duplicate investigation.

Only link questions that are genuinely useful decompositions, not merely topically related. Also, ensure that a good answer to the child question would add substantial information to the parent question, even in the presence of good answers to all other questions in the workspace. If, given answers to another set of questions in the workspace, a question's answer would not add much further value, do not link it. Think of it like a Bayesian netowrk: aim to capture conditional independence relations with your links.

## Hypothesis Questions

When you have a compelling candidate answer or paradigm — not just a piece of evidence, but a specific view that, if true, would substantially shape the response to the question — propose a hypothesis. This is worth doing when the view is likely correct, or when engaging with it seriously might yield useful insights: clarifying why it fails, surfacing adjacent territory, or extracting the partial truth inside an otherwise wrong answer.

Don't propose a hypothesis if the view is already well-represented in the existing consideration set, or if it's a restatement of the question itself. One good hypothesis beats several thin ones.

## Quality Bar

- **One excellent consideration beats three weak ones.** If you can only find one genuinely important missing angle, produce one.
- **Specificity is essential.** A claim must be a concrete, falsifiable (or at least evaluable) assertion — not a gesture toward a class of considerations.
- **Do not restate existing considerations** in different words.
