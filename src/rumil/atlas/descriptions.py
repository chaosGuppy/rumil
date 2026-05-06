"""Canonical natural-language descriptions for PageType / CallType enums.

These mirror what ``prompts/preamble.md`` says about each page type and
what the per-call prompt files say about each call type. Keeping them
next to the code (rather than only in the preamble) means the atlas /
registry UI can render exactly the same prose the LLM reads, and a
description-completeness test can hold every enum value to a non-empty
description.
"""

from rumil.models import CallType, PageLayer, PageType, Workspace

PAGE_TYPE_DESCRIPTIONS: dict[PageType, str] = {
    PageType.CLAIM: (
        "Positive assertion about the world, specific enough that a credence "
        "(how likely is this to be true) is meaningfully assignable. Vague "
        "gestures aren't claims — make them questions, judgements, or view "
        "items instead."
    ),
    PageType.QUESTION: (
        "Something the workspace is investigating. The headline carries the "
        "question; the content is for disambiguation (scope, units, what "
        "counts as an answer), not for investigation strategy."
    ),
    PageType.JUDGEMENT: (
        "Current best take on a question. Carries robustness, no credence "
        '(a judgement is the considered answer; "how likely is this to be '
        'true" is the wrong frame for it).'
    ),
    PageType.SOURCE: (
        "Ingested document, scraped from a URL or imported from a file. "
        "Created by the system rather than by the agent during research."
    ),
    PageType.WIKI: (
        "Reusable concept page — a stable definition or framing the rest of "
        "the workspace can cite. Distinct from claims (which assert) and "
        "questions (which investigate)."
    ),
    PageType.SUMMARY: (
        "Distilled summary produced by a summarize call. Sits below the "
        "considered judgements but above raw considerations."
    ),
    PageType.VIEW: (
        "Structured summary of current understanding on a question. "
        "Contains atomic view items in sections (broader context, confident "
        "views, live hypotheses, key evidence, assessments, key "
        "uncertainties). When a question has a view, the view is the "
        "primary context shown to instances working on that question."
    ),
    PageType.VIEW_ITEM: (
        "Atomic observation inside a view, scored on robustness and "
        "importance. Sharp credence-apt assertions inside a view should be "
        "split out as separate claims and cited from the view item."
    ),
    PageType.VIEW_META: (
        "Internal bookkeeping for a view (e.g. provenance pointers, "
        "scratch). Not surfaced to readers as part of the view's content."
    ),
    PageType.SPEC_ITEM: (
        "Prescriptive constraint on a generated artefact. Used by the "
        "generative workflow: each spec item is a should/should-not the "
        "artefact must respect."
    ),
    PageType.ARTEFACT: (
        "Long-form object produced by the generative workflow — the actual "
        "deliverable being drafted, critiqued, and revised against the spec."
    ),
}


CALL_TYPE_DESCRIPTIONS: dict[CallType, str] = {
    CallType.FIND_CONSIDERATIONS: (
        "Surface the handful of considerations that would most move the "
        "next answer on a question. A targeted sharpener — explicitly "
        "avoids opening new lines of investigation whose payoff is deferred."
    ),
    CallType.ASSESS: (
        "Produce a judgement on a question, integrating the considerations "
        "currently in scope. Renders the workspace's current best answer."
    ),
    CallType.PRIORITIZATION: (
        "Plan what to do next. Reads the question's research state and "
        "dispatches a budgeted set of follow-up calls (scouts, web "
        "research, recurses, etc.)."
    ),
    CallType.INGEST: (
        "Extract considerations and questions from a Source page into the "
        "workspace, linked back to the source for citation."
    ),
    CallType.REFRAME: (
        "Reformulate a question that's been judged poorly framed — split "
        "scope, sharpen units, or replace it with a better-defined "
        "successor."
    ),
    CallType.MAINTAIN: (
        "Workspace hygiene call: dedupe pages, fix stale links, retire "
        "superseded content. Run periodically or after large research bursts."
    ),
    CallType.SUMMARIZE: (
        "Distill a question's research state into a Summary page. Reads broadly, writes once."
    ),
    CallType.SCOUT_SUBQUESTIONS: (
        "Specialized scout that identifies informative subquestions for "
        "the scope question — questions whose answers would meaningfully "
        "move the parent."
    ),
    CallType.SCOUT_ESTIMATES: (
        "Specialized scout that generates quantitative estimates bearing "
        "on the scope question — Fermi estimates, base rates, magnitude "
        "checks."
    ),
    CallType.SCOUT_HYPOTHESES: (
        "Specialized scout that proposes competing hypotheses for the "
        "scope question. Aimed at coverage of plausible answers, not "
        "depth on any one."
    ),
    CallType.SCOUT_ANALOGIES: (
        "Specialized scout that finds illuminating analogies for the "
        "scope question — reference cases from adjacent domains whose "
        "structure transfers."
    ),
    CallType.SCOUT_PARADIGM_CASES: (
        "Specialized scout that identifies concrete paradigm cases — "
        "real, named, dated instances of the same phenomenon the "
        "question is about."
    ),
    CallType.SCOUT_FACTCHECKS: (
        "Specialized scout that surfaces uncertain factual claims whose "
        "truth value could materially affect the answer to the scope "
        "question."
    ),
    CallType.SCOUT_WEB_QUESTIONS: (
        "Specialized scout that identifies concrete factual questions "
        "answerable via web research, where the LLM does not already know "
        "the answer."
    ),
    CallType.SCOUT_DEEP_QUESTIONS: (
        "Specialized scout that identifies important questions requiring "
        "judgement, interpretation, or involved reasoning — questions "
        "that cannot be resolved by simply looking something up."
    ),
    CallType.SCOUT_C_HOW_TRUE: (
        "Claim-investigation scout: generate evidence and arguments for "
        "why the focal claim is likely true."
    ),
    CallType.SCOUT_C_HOW_FALSE: (
        "Claim-investigation scout: generate evidence and arguments for "
        "why the focal claim might be false."
    ),
    CallType.SCOUT_C_CRUXES: (
        "Claim-investigation scout: identify cruxes — sub-claims whose "
        "truth value would flip the focal claim's credence."
    ),
    CallType.SCOUT_C_RELEVANT_EVIDENCE: (
        "Claim-investigation scout: enumerate concrete evidence that "
        "bears on the focal claim, regardless of which direction it "
        "points."
    ),
    CallType.SCOUT_C_STRESS_TEST_CASES: (
        "Claim-investigation scout: generate edge cases / scenarios that "
        "stress-test the focal claim."
    ),
    CallType.SCOUT_C_ROBUSTIFY: (
        "Claim-investigation scout: improve the claim's framing or scope "
        "so that its credence is more defensible."
    ),
    CallType.SCOUT_C_STRENGTHEN: (
        "Claim-investigation scout: build out the supporting argument "
        "structure for the focal claim — citations, sub-claims, "
        "qualifications."
    ),
    CallType.WEB_RESEARCH: (
        "Run server-side web search + scrape, ingesting findings as "
        "considerations linked to source pages. Used for facts the model "
        "doesn't already know."
    ),
    CallType.EVALUATE: (
        "Run an evaluation pass over a question or claim's research, "
        "producing a structured verdict."
    ),
    CallType.GROUNDING_FEEDBACK: (
        "Identify which existing claims are affected by new feedback / "
        "evaluation and queue them for re-assessment."
    ),
    CallType.FEEDBACK_UPDATE: (
        "Apply queued feedback updates to the affected pages — revisions, "
        "supersessions, link changes."
    ),
    CallType.LINK_SUBQUESTIONS: (
        "Linker pass that proposes child-question links between existing "
        "questions where the structural relationship is missing."
    ),
    CallType.AB_EVAL: (
        "A/B evaluation harness call — runs both arms and emits per-dimension comparisons."
    ),
    CallType.AB_EVAL_COMPARISON: (
        "Per-dimension comparison call inside an A/B evaluation — picks "
        "a preference and explains why."
    ),
    CallType.AB_EVAL_SUMMARY: (
        "Final write-up call inside an A/B evaluation — aggregates the "
        "per-dimension comparisons into an overall report."
    ),
    CallType.RUN_EVAL: (
        "Run-level evaluation — assess the full research output of a "
        "completed run against quality dimensions."
    ),
    CallType.CREATE_VIEW: (
        "Build a fresh View page on a question by reading the current "
        "research subgraph and emitting a structured set of view items."
    ),
    CallType.CREATE_VIEW_MAX_EFFORT: (
        "Like CREATE_VIEW but with a higher context budget and more "
        "thorough rendering — the slow, deliberate variant."
    ),
    CallType.GLOBAL_PRIORITIZATION: (
        "Workspace-wide prioritization pass that picks which root "
        "questions to invest budget in next, given the current state of "
        "all open investigations."
    ),
    CallType.UPDATE_VIEW: (
        "Refresh an existing View on a question after new research has "
        "landed — preserve unchanged items, revise stale ones, add new "
        "ones."
    ),
    CallType.UPDATE_VIEW_MAX_EFFORT: (
        "Like UPDATE_VIEW but with a higher context budget — used after "
        "substantial research bursts where light touch isn't enough."
    ),
    CallType.CREATE_FREEFORM_VIEW: (
        "Build a freeform (prose) view on a question, as opposed to the "
        "structured view-items format."
    ),
    CallType.UPDATE_FREEFORM_VIEW: (
        "Refresh an existing freeform view after new research has landed."
    ),
    CallType.GENERATE_SPEC: (
        "Generative workflow call that turns a brief into spec items — "
        "constraints the artefact must respect."
    ),
    CallType.GENERATE_ARTEFACT: (
        "Generative workflow call that drafts the long-form artefact against the current spec."
    ),
    CallType.CRITIQUE_ARTEFACT: (
        "Generative workflow call that critiques the current draft "
        "artefact against the spec, surfacing gaps and revisions."
    ),
    CallType.CRITIQUE_ARTEFACT_REQUEST_ONLY: (
        "Variant of CRITIQUE_ARTEFACT that emits change requests only (no rewriting)."
    ),
    CallType.REFINE_SPEC: (
        "Generative workflow call that revises spec items in light of "
        "what the draft revealed about the spec's coverage."
    ),
    CallType.RED_TEAM: (
        "Adversarial pass on a question's current judgement — argue the "
        "opposite, surface missed considerations, propose stress tests."
    ),
    CallType.CLAUDE_CODE_DIRECT: (
        "Envelope call for mutations made from Claude Code's broader "
        "context — not a rumil-internal call with a carefully scoped "
        "prompt. Never dispatchable from prioritization."
    ),
    CallType.VERSUS_JUDGE: (
        "Pairwise judgment between two essay continuations, driven by "
        "the external versus harness. Uses single-arm workspace-"
        "exploration tools; doesn't write to ab_eval_reports."
    ),
    CallType.VERSUS_COMPLETE: (
        "Essay continuation produced by the external versus harness — "
        "either single-shot or via a multi-stage Workflow like "
        "DraftAndEdit. Distinct from VERSUS_JUDGE so completion runs "
        "don't pollute judge analytics."
    ),
    CallType.CONTEXT_BUILDER_EVAL: (
        "Lightweight build_context-only call used by the context-builder "
        "evaluation workflow. Each eval run has one of these calls — "
        "gold (ImpactFilteredContext) or candidate (named builder under "
        "test). Never dispatchable from prioritization."
    ),
}


PAGE_LAYER_DESCRIPTIONS: dict[PageLayer, str] = {
    PageLayer.WIKI: (
        "Stable, citable, broadly-applicable knowledge — wiki pages and "
        "long-lived reference content."
    ),
    PageLayer.SQUIDGY: (
        "Project-specific, in-flight research — claims, questions, "
        "judgements, views. Where the day-to-day workspace lives."
    ),
}


WORKSPACE_DESCRIPTIONS: dict[Workspace, str] = {
    Workspace.RESEARCH: (
        "Default workspace — actual research pages (claims, questions, "
        "judgements, views, sources, etc.)."
    ),
    Workspace.PRIORITIZATION: (
        "Internal layer for prioritization-call outputs (planner pages, "
        "scoring records). Kept separate so prio bookkeeping doesn't "
        "pollute embedding search over real research."
    ),
}
