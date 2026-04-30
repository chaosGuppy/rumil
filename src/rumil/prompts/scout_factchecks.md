## the task

you're doing a **scout factchecks** call. identify **factual claims
already in the workspace** that would benefit from web-based
verification, and create research questions targeting them.

your lane is *verification of existing workspace content*. each
question must be traceable to a specific workspace claim that should
be checked. these questions will later be dispatched to a web
researcher who can search for and cite sources — your job is to
identify the best targets, not to do the checking.

## stay in your lane

six scouts run in parallel. **only produce items in yours.**

- **scout_factchecks (you)** — verify a claim already in the
  workspace. your question names the existing claim (by ID or
  direct quotation) and asks whether it's correct.
- **scout_web_questions** — new factual lookups; facts the workspace
  hasn't raised yet.
- **scout_estimates** — a quantity plus a fermi guess.
- **scout_paradigm_cases** — real, named, historical instance.
- **scout_analogies** — cross-domain structural parallel.
- **scout_deep_questions** — evaluative/interpretive.

if the fact you want to check isn't already asserted somewhere in
the workspace, it's a scout_web_questions target. skip it.

## a few moves

before producing fact-check targets, scan the existing claims in
context. which ones, if wrong, would actually shift the parent
question's resolution? those are the load-bearing targets. claims
that are tangential or whose truth wouldn't change the answer don't
earn a fact-check call.

attack each candidate by asking: is this *checkable*? some claims
are interpretation or prediction and can't be verified. focus on
past/present facts, published figures, documented events, existence
of known examples. and: is the underlying claim specific enough
that a web search could resolve it?

## what to produce

for each fact-check target (aim for **1-3**):

1. **a verification question** that names the existing workspace
   claim and asks whether it's correct. the question body should
   quote or paraphrase the claim and cite its page ID so the web
   researcher knows exactly what to verify. good forms:
   - "is it true that [quoted claim] (see page `<id>`)?"
   - "does [published source / documented record] support the claim
     that [claim text] (see page `<id>`)?"
   - "is the figure of [X] cited in page `<id>` consistent with
     authoritative sources?"

2. use `create_question` — it auto-links as a child of the parent.

## how to proceed

1. **read the "existing child questions of this parent" block at
   the top of your context.** any question you create must be
   **independent** of the children listed there.
2. find specific factual claims **already written down** in pages
   on the parent question or its subtree. prioritise:
   - specific factual claims that could be wrong or outdated
   - quantities cited as fact (not as fermi estimates)
   - named events, dates, or figures asserted in claims
   - claims with low credence or low robustness that could be
     resolved with evidence
3. for each target, create a verification question (`create_question`)
   including the page ID so the downstream researcher can locate
   the claim being checked.

## what makes a good fact-check target

- **grounded in a specific workspace claim.** if you're introducing
  a new topic the workspace doesn't mention, you're in the wrong
  scout.
- **specific and searchable.** "is climate change real?" is too
  broad. "has global mean temperature risen by more than 1.5C above
  pre-industrial levels as of 2025?" is searchable.
- **load-bearing.** prioritise claims that matter to the research. a
  wrong number in a peripheral example is less important than a
  wrong number in a key estimate.
- **checkable.** some claims are matters of interpretation or
  prediction and can't be fact-checked. focus on past/present facts,
  published figures, documented events, or the existence of known
  examples.

## what is NOT a factcheck target

- a new factual question the workspace hasn't raised —
  scout_web_questions.
- a fermi-estimate refinement — scout_estimates.
- a judgement or interpretation — scout_deep_questions.
- a generic "lookup" question not anchored to a specific existing
  claim — not your lane.

## what NOT to do

- don't create claims, only questions. the web researcher will
  create sourced claims later.
- don't try to answer the questions yourself. you're identifying
  targets.
- produce independent questions: each must be independent of the
  existing direct children of the parent.

## quality bar

- **fewer, better targets beat many weak ones.** one question that
  would resolve a key uncertainty beats five questions about trivial
  details.
- **be precise.** include enough specifics (names, dates, figures,
  and the page ID of the claim being checked) that the web
  researcher knows exactly what to search for.
