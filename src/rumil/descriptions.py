"""One-line descriptions for CallType and MoveType enum members.

Surfaced through ``/api/capabilities`` and the orchestrator info popover
so UI chips can show a tooltip ("what does this call type do?") without
the reader having to grep the prompts.

Kept as a plain dict rather than attached to the enum values so enums
stay stringy-by-value and the description registry can grow / evolve
without touching ``models.py``. A pytest asserts every enum member has
an entry — if you add a new CallType / MoveType and forget to describe
it, the test fails.
"""

from rumil.models import CallType, MoveType

CALL_TYPE_DESCRIPTIONS: dict[CallType, str] = {
    CallType.FIND_CONSIDERATIONS: (
        "General-purpose expansion of a question — surface relevant claims "
        "and sub-questions from the model's knowledge."
    ),
    CallType.ASSESS: (
        "Rate credence and robustness on a question or claim; optionally create a judgement."
    ),
    CallType.PRIORITIZATION: (
        "LLM picks the next batch of calls to dispatch against the root "
        "question, given current state."
    ),
    CallType.INGEST: (
        "Extract considerations from an attached source (URL / PDF / file) onto a target question."
    ),
    CallType.REFRAME: "Reframe a question — propose alternative phrasings.",
    CallType.MAINTAIN: "Maintenance pass — clean up or consolidate workspace state.",
    CallType.SCOUT_SUBQUESTIONS: "Scout variant: generate sub-questions to decompose the question.",
    CallType.SCOUT_ESTIMATES: "Scout variant: surface quantitative estimates relevant to the question.",
    CallType.SCOUT_HYPOTHESES: "Scout variant: enumerate candidate hypotheses.",
    CallType.SCOUT_ANALOGIES: "Scout variant: surface analogous cases / reference classes.",
    CallType.SCOUT_PARADIGM_CASES: "Scout variant: canonical paradigm cases relevant to the question.",
    CallType.SCOUT_FACTCHECKS: "Scout variant: factual claims worth verifying.",
    CallType.SCOUT_WEB_QUESTIONS: "Scout variant: sub-questions best answered by web research.",
    CallType.SCOUT_DEEP_QUESTIONS: "Scout variant: go deeper on the most load-bearing sub-questions.",
    CallType.SCOUT_C_HOW_TRUE: "Claim-scout: what would make this claim true.",
    CallType.SCOUT_C_HOW_FALSE: "Claim-scout: what would make this claim false.",
    CallType.SCOUT_C_CRUXES: "Claim-scout: identify cruxes the claim depends on.",
    CallType.SCOUT_C_RELEVANT_EVIDENCE: "Claim-scout: surface evidence bearing on the claim.",
    CallType.SCOUT_C_STRESS_TEST_CASES: "Claim-scout: edge cases that stress-test the claim.",
    CallType.SCOUT_C_ROBUSTIFY: "Claim-scout: strengthen claim by narrowing or qualifying.",
    CallType.SCOUT_C_STRENGTHEN: "Claim-scout: make the claim more defensible.",
    CallType.WEB_RESEARCH: (
        "Live web search + scrape; produce considerations grounded in fetched sources."
    ),
    CallType.EVALUATE: "Evaluation call — an eval agent reviews a run.",
    CallType.GROUNDING_FEEDBACK: "Grounding-pipeline feedback call.",
    CallType.FEEDBACK_UPDATE: "Apply feedback-pipeline suggestions to the workspace.",
    CallType.LINK_SUBQUESTIONS: "Wire newly-created sub-questions into the graph.",
    CallType.AB_EVAL: "A/B eval: compare two runs on the same question.",
    CallType.AB_EVAL_COMPARISON: "A/B eval sub-step: produce a single comparison verdict.",
    CallType.AB_EVAL_SUMMARY: "A/B eval sub-step: summarise all comparisons.",
    CallType.RUN_EVAL: "Single-run evaluation call.",
    CallType.SINGLE_CALL_BASELINE: "Baseline: answer the question in one LLM call (no graph).",
    CallType.CREATE_VIEW: "Create the distillation view page for a question.",
    CallType.GLOBAL_PRIORITIZATION: "Cross-question prioritization: pick which question to work on.",
    CallType.UPDATE_VIEW: "Refresh the distillation view after new evidence.",
    CallType.CHAT_DIRECT: "Envelope for chat-dispatched mutations (not a prompted call).",
    CallType.ADVERSARIAL_REVIEW: "Adversarially review a drafted artifact; find weaknesses.",
    CallType.EXPLORE_TENSION: "Investigate a detected tension between two high-credence claims.",
    CallType.DRAFT_ARTIFACT: "Draft an artifact (essay / summary) for later review.",
    CallType.BUILD_MODEL: "Build a structured (theoretical) model of the question.",
    CallType.CLAUDE_CODE_DIRECT: "Envelope for Claude Code-dispatched mutations.",
    CallType.AUTHOR_INLAY: "Reserved: model-authored Inlay page for a question's custom view.",
}

MOVE_TYPE_DESCRIPTIONS: dict[MoveType, str] = {
    MoveType.CREATE_CLAIM: "Create a new claim page.",
    MoveType.CREATE_QUESTION: "Create a new sub-question page.",
    MoveType.CREATE_SCOUT_QUESTION: "Create a sub-question tagged as a scout target.",
    MoveType.CREATE_JUDGEMENT: "Create a judgement answering a question.",
    MoveType.CREATE_WIKI_PAGE: "Create a wiki page for reusable context.",
    MoveType.LINK_CONSIDERATION: "Link a claim as a consideration bearing on a question.",
    MoveType.LINK_CHILD_QUESTION: "Link a sub-question as a child of another question.",
    MoveType.LINK_RELATED: "Add a general 'related' link between two pages.",
    MoveType.LINK_VARIANT: "Mark one claim as a more-robust variant of another.",
    MoveType.FLAG_FUNNINESS: "Flag a page as funny / interesting.",
    MoveType.FLAG_ISSUE: "Flag a page as having an issue needing review.",
    MoveType.REPORT_DUPLICATE: "Report that two pages duplicate each other.",
    MoveType.LOAD_PAGE: "Load a page's full content into the current call's context.",
    MoveType.REMOVE_LINK: "Remove a link between two pages.",
    MoveType.CHANGE_LINK_ROLE: "Change a link's role (e.g. consideration direction).",
    MoveType.UPDATE_EPISTEMIC: "Update a page's credence or robustness.",
    MoveType.CREATE_VIEW_ITEM: "Create a view item under a distillation view.",
    MoveType.PROPOSE_VIEW_ITEM: "Propose (not yet confirmed) a view item.",
    MoveType.ANNOTATE_SPAN: "Annotate a span within a page.",
    MoveType.ANNOTATE_ALTERNATIVE: "Annotate an alternative phrasing for a page.",
    MoveType.WRITE_MODEL_BODY: "Write the body of a model page.",
}
