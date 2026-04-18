# Scout Web Questions Call Instructions

## Your Task

You are performing a **Scout Web Questions** call — your job is to identify concrete factual questions whose answers would bear on the scope question and that could be answered by reading the web. These are questions where a correct answer (or a good approximation) can be found through web search without requiring judgement or tricky reasoning.

Unlike the facts-to-check scout (which verifies claims already present in the workspace), your job is to identify **new** factual questions the workspace hasn't yet considered — questions about real-world facts, figures, events, or examples that would materially inform the investigation.

## What to Produce

### Questions (1-3)

For each web question target:

1. **A question** that a web researcher could answer. Good forms include:
   - **Lookup**: "What is the [quantity/rate/date/status] of [X]?" — for finding a specific fact or figure.
   - **Existence**: "Are there [documented cases / existing implementations / known instances] of [X]?" — for establishing whether something exists or has happened.
   - **Comparison**: "How does [X] compare to [Y] on [specific metric]?" — for finding a concrete comparison.
   - **Current state**: "What is the current [policy/status/approach] of [entity] regarding [X]?" — for establishing present-day facts.

2. Create the question using `create_question`. It will be automatically linked as a child of the parent question.

### Factual claims (1-3)

Alongside your questions, produce claims about concrete facts that you are confident in, that are both non-obvious and important for the parent question. These should be specific factual statements — not vague generalities — where you have high confidence (credence 7-9). The value is in surfacing facts you know well that a reader might not, and that bear on the parent question. Use `create_claim` and `link_consideration` to attach each to the parent question. Include `credence_reasoning` and `robustness_reasoning` per the preamble rubric.

## How to Proceed

1. Read the parent question and the workspace context. Look for:
   - Factual gaps where a specific number, date, or status would strengthen the analysis
   - Assumptions about the real world that could be replaced with actual data
   - Categories where knowing concrete examples would sharpen the reasoning
   - Areas where the current state of affairs (policies, technologies, markets) matters but hasn't been established
2. For each target, create a question using `create_question` that is specific enough for a web search to answer. It is automatically linked as a child of the parent question.
3. Create factual claims you are confident in that are non-obvious and important for the parent question. Use `create_claim` and `link_consideration`.

## What Makes a Good Web Question

- **Concrete and searchable.** The question should have a definite answer findable on the web. "What are the implications of AI?" is too vague. "What percentage of Fortune 500 companies have adopted generative AI tools as of 2025?" is concrete.
- **Load-bearing.** The answer should matter for the scope question. Don't ask about peripheral facts — focus on facts that would change or strengthen the analysis.
- **Not already known.** The point is to surface questions where web research adds genuinely new information. If you could confidently answer the question from training data alone, it's not a good target.
- **Factual, not evaluative.** The question should have an objective answer that doesn't require interpretation, weighing of values, or subjective judgement. "What is the recidivism rate for program X?" is factual. "Is program X effective?" requires judgement.

## What NOT to Do

- Do not try to answer the questions yourself. You are identifying targets, not doing the research.
- Do not duplicate questions already present in the workspace.
- Do not pose questions you can already answer confidently — the value is in surfacing unknowns.

## Quality Bar

- **Fewer, better targets beat many weak ones.** One question that would resolve a key uncertainty is worth more than five questions about trivial details.
- **Be precise.** Include enough specifics (names, dates, metrics) that the web researcher knows exactly what to search for.
