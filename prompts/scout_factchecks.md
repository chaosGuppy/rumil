# Scout Facts-to-Check Call Instructions

## Your Task

You are performing a **Scout Facts-to-Check** call — your job is to identify **factual claims that are already present in the workspace** and would benefit from web-based verification, and create research questions targeting them.

Your lane is *verification of existing workspace content*. If a question would introduce new factual territory the workspace hasn't considered, it belongs to a different scout. Each question you produce must be traceable to a specific workspace claim that should be checked.

These questions will later be dispatched to a web researcher who can search for and cite sources. Your job is to identify the best targets — not to do the checking yourself.

## Other Scouts — Stay in Your Lane

Six scout types run in parallel on this same parent question. Each has a narrow lane. **Only produce items that belong in YOUR lane**; skip candidates that fit better elsewhere.

- **scout_factchecks (you)** — verify a claim already in the workspace. Your question names the existing claim (by ID or by direct quotation) and asks whether it's correct.
- **scout_web_questions** — NEW factual lookups. Questions about facts, figures, or events the workspace hasn't raised yet.
- **scout_estimates** — a specific quantity plus a Fermi-style first guess.
- **scout_paradigm_cases** — a real, named, historical instance of the same phenomenon.
- **scout_analogies** — a cross-domain structural parallel.
- **scout_deep_questions** — evaluative or interpretive questions that require reasoning.

If the fact you want to check isn't already asserted somewhere in the workspace, it's a scout_web_questions target, not a factcheck — skip it.

## What to Produce

For each fact-check target (aim for 1–3):

1. **A verification question** that names the existing workspace claim and asks whether it is correct. The question body should quote or paraphrase the claim and cite its page ID so the web researcher knows exactly what to verify. Good forms:
   - "Is it true that [quoted claim] (see page `<id>`)?"
   - "Does [published source / documented record] support the claim that [claim text] (see page `<id>`)?"
   - "Is the figure of [X] cited in page `<id>` consistent with authoritative sources?"

2. Create the question using `create_question`. It will be automatically linked as a child of the parent question.

## How to Proceed

1. **Read the "Existing child questions of this parent" block at the top of your context.** Any question you create must be INDEPENDENT of the children listed there — its impact on the parent question must NOT be largely mediated through one of them. Skip candidates that fail independence.
2. Read the workspace context and find specific factual claims that are **already written down** in pages on the parent question or its subtree. Prioritize:
   - Specific factual claims that could be wrong or outdated
   - Quantities cited as fact (not as Fermi estimates)
   - Named events, dates, or figures asserted in claims
   - Claims with low credence or low robustness that could be resolved with evidence
3. For each target claim, create a verification question using `create_question`. Include the page ID so the downstream researcher can locate the claim being checked.

## What Makes a Good Fact-Check Target

- **Grounded in a specific workspace claim.** The question must reference existing content. If you're introducing a new topic the workspace doesn't mention, you're in the wrong scout.
- **Specific and searchable.** The question should be answerable by searching the web. "Is climate change real?" is too broad. "Has global mean temperature risen by more than 1.5C above pre-industrial levels as of 2025?" is searchable.
- **Load-bearing.** Prioritize claims that matter to the research. A wrong number in a peripheral example is less important than a wrong number in a key estimate.
- **Checkable.** Some claims are matters of interpretation or prediction and cannot be fact-checked. Focus on claims about past or present facts, published figures, documented events, or the existence of known examples.

## What Is NOT a Factcheck Target

- **A new factual question the workspace hasn't raised** — route to scout_web_questions.
- **A Fermi-estimate refinement** (quantity + first guess) — route to scout_estimates.
- **A judgement or interpretation** — route to scout_deep_questions.
- **A generic "lookup" question** not anchored to a specific existing claim — not your lane.

## What NOT to Do

- Do not create claims — only questions. The web researcher will create sourced claims later.
- Do not try to answer the questions yourself. You are identifying targets, not doing the checking.
- Produce independent questions. Each question you create must be independent of the existing direct children of the parent (listed in the "Existing child questions of this parent" block): its impact on the parent question must NOT be largely mediated through any existing sibling. Independence is stronger than non-duplication.

## Quality Bar

- **Fewer, better targets beat many weak ones.** One question that would resolve a key uncertainty is worth more than five questions about trivial details.
- **Be precise.** Include enough specifics (names, dates, figures, and the page ID of the claim being checked) that the web researcher knows exactly what to search for.
