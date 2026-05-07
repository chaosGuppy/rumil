"""DraftAndEditWorkflow — SDK-driven essay completion via draft → critique → edit.

Distinct from the BudgetedOrchWorkflow base because there's no rumil
orchestrator wrapped here: the workflow drives a small fixed pipeline
of plain ``text_call`` LLMs (drafter → N parallel critics → editor) per
round and stores the final draft on ``question.content`` for the
versus runner's ``produces_artifact=True`` path to read.

Design notes (full sketch in ``planning/draft-and-edit-workflow-sketch.md``):

- **Spawn pattern**: ``asyncio.gather`` over ``text_call`` per critic.
  Critics need no tools, no autonomy, and benefit from per-role model
  overrides — the SDK's nested ``Agent`` tool would be heavier than
  warranted here.
- **Where intermediates live**: trace events (``DraftEvent``,
  ``CritiqueRoundEvent``, ``EditEvent``) on the workflow's call —
  not workspace pages. Critic prose on the page graph would pollute
  embedding search and risks leaking essay prefix material into
  unrelated workspace surfaces under blind-judging.
- **Where the final draft lives**: ``question.content`` via
  ``db.update_page_content`` (mutation event aware). The runner reads
  it verbatim and feeds it to ``CompleteEssayTask.extract_artifact``.
- **Budget**: 1 unit per outer round. One round = drafter (or editor)
  + N critics. Budget consumed at the top of each round; if exhausted
  before any draft was produced ``last_status="incomplete"``.
- **Per-role models**: drafter / critic / editor models can differ via
  constructor kwargs; ``None`` means "inherit the rumil_model_override
  from settings", which is what ``run_versus`` sets from the caller's
  ``--model``.
- **Per-stage prompt overrides**: each role's prompt may be replaced
  by passing a path to a markdown file (``drafter_prompt_path`` /
  ``critic_prompt_path`` / ``editor_prompt_path``). When unset, the
  built-in ``_DEFAULT_*_PROMPT`` constants are used. The fingerprint
  hashes the actual loaded text so two variants pointed at the same
  content via different paths fingerprint identically. This is the
  iterate skill's primary lever for A/B-ing prompt-text variants
  without forking the workflow file.
"""

import asyncio
import dataclasses
import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from rumil.budget import _consume_budget
from rumil.calls.common import mark_call_completed
from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, text_call
from rumil.model_config import ModelConfig
from rumil.models import CallStatus, CallType
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import (
    ArbitrationEvent,
    ArbitrationStartedEvent,
    BriefAuditEvent,
    BriefAuditStartedEvent,
    CritiqueItem,
    CritiqueRoundEvent,
    CritiqueStartedEvent,
    DraftEvent,
    DraftStartedEvent,
    EditEvent,
    EditStartedEvent,
    PlannerEvent,
    PlannerStartedEvent,
    RoundStartedEvent,
    ScoutPassEvent,
    ScoutPassStartedEvent,
)
from rumil.tracing.tracer import CallTrace, reset_trace, set_trace

_DEFAULT_DRAFTER_PROMPT = (
    "You are continuing an essay. The user message will give you the "
    "opening of an essay (the prefix) plus a target length. Your job is "
    "to write a substantive continuation that picks up the opening's "
    "argumentative thread.\n\n"
    "Match the opening's voice and register. Advance the argument — "
    "don't restate the opening, don't hedge performatively, don't drift "
    "generic.\n\n"
    "**Length is a hard ceiling, not a floor.** The user message gives "
    "a target character count. Treat that target as a *maximum*, not a "
    "minimum. Going materially over is a failure mode — most drafts "
    "that overshoot are padded with restatement, hedging, or "
    "under-developed elaboration. A tight draft at 80% of target beats "
    "a sprawling draft at 130%.\n\n"
    "Before the continuation, output a one-line plan in this form:\n"
    "  Plan: ~N chars, M moves: <comma-separated moves>\n"
    "where N is your self-set budget (at-or-under target) and M is the "
    "number of distinct argumentative moves you intend to make. Then "
    "write the continuation, staying at-or-under the planned N.\n\n"
    "Wrap the final continuation in <continuation>...</continuation> "
    "tags. Scratch space before the tagged block (including the Plan "
    "line) is fine; only the content inside the tags is kept."
)


_DEFAULT_CRITIC_PROMPT = (
    "You are reviewing a draft essay continuation. The user message "
    "will give you the essay opening (the prefix), the current "
    "draft continuation, and a length status (current vs target "
    "characters). Identify problems: weak arguments, factual errors, "
    "style mismatches, missed opportunities, places where the draft "
    "drifts from the opening's thread or tone. Be specific — name "
    "passages, quote phrases, point at concrete moves the writer "
    "could make.\n\n"
    "**Length awareness.** When the draft is at or above target, "
    "prefer cut suggestions over expansion suggestions — quote "
    "specific paragraphs or passages to drop, identify ideas that "
    "could be stated once instead of restated, flag tangents the "
    "piece doesn't need. Critics that only suggest additions push "
    "the editor into runaway expansion. When the draft is "
    "meaningfully below target, expansion suggestions are fine. "
    "**Calibrate intensity to the length delta.** If the draft is "
    "within ±5% of target and reads cleanly, surface only the 1-3 "
    "highest-impact issues; do not manufacture a full punch list "
    "when the draft is essentially fine. Late-round editor over-"
    "correction has been observed when critics produce a long "
    "issue list against a near-target draft.\n\n"
    "You're not writing the next draft — an editor will read your "
    "critique and decide what to act on. Don't hedge; don't pad with "
    "praise; don't restate what the draft already does. If a section "
    "works, it's fine to skip it.\n\n"
    "**Output format.** Free-form prose, EXCEPT every cut suggestion "
    "must be a verbatim quote of the phrase or sentence to be removed. "
    "The editor's downstream <cuts> block requires verbatim phrases; "
    'if you describe cuts by paraphrase or section name ("the third '
    'paragraph of section 2", "the calibration discussion") the '
    "editor has to invent the quotes itself and the cut targets drift. "
    "Use this shape for cuts:\n\n"
    '  Cut: "<verbatim phrase or sentence from the draft>" '
    "— Reason: <one-sentence why>\n\n"
    "Quote in full when feasible. Multiple Cut: lines per critique "
    "are fine. Other observations (style, structure, missing arguments) "
    "stay free-form."
)


_DEFAULT_EDITOR_PROMPT = (
    "You are revising a draft essay continuation. The user message "
    "will give you the essay opening (the prefix), the current draft, "
    "and a set of critiques from independent reviewers. Produce a "
    "revised continuation that incorporates the most important "
    "improvements while preserving what worked.\n\n"
    "**Push back on critics when they're wrong.** Critics sometimes "
    "demand changes that would hurt the piece — they may attack a move "
    "that's actually correct, push toward generic prose, or pull in "
    "incompatible directions. You are the final author. If a critic's "
    "suggestion would weaken the draft, ignore it. If two critics "
    "disagree, pick the one whose reading is closer to the opening's "
    "actual argument. Don't whiplash to satisfy every note. State "
    "briefly which critic notes you're acting on and which you're "
    "declining (and why).\n\n"
    "**Length discipline.** The user message gives both the current "
    "draft length and the target length. If the current draft is at or "
    "above target, your job is to TIGHTEN. Cutting is the primary "
    "edit. The revised continuation must be at-or-under the target. "
    "If current is close to target, edit at roughly neutral length. "
    "Only expand when the current draft is meaningfully below target "
    "and a critic identified a missing argument worth adding. "
    "**Tightening from anti-tells should produce a SHORTER revision, "
    "not a longer one** — if you cut scaffolding sentences and add "
    "nothing, the revision must be shorter than the current draft, "
    "not the same length or longer.\n\n"
    "**Sentence-substance check.** A lot of professional-sounding "
    "writing is 80% scaffolding and 20% claim — sentences that read "
    "smoothly but don't constrain the answer. Yours especially, "
    "since you're trained to produce well-structured output without "
    "that structure necessarily reflecting actual reasoning. Two "
    "moves to fight this:\n\n"
    "  1. **For each <preserved> section**, name in one line the "
    "specific argumentative claim it makes that would NOT survive "
    "deletion. If you can't articulate the claim a deletion would "
    "lose, the section is scaffolding — cut it instead of "
    "preserving. Don't preserve sections on the basis that they "
    "'read cleanly' or 'capture the voice well.'\n"
    "  2. **For each new or substantially-revised sentence in the "
    "revision**, internally ask: 'if I deleted this sentence, would "
    "the section's argumentative claim change?' Keep the sentence "
    "only if the answer is yes. Don't pad transitions, restate "
    "adjacent sentences in different words, or gesture with "
    "'important to note' / 'raises questions' / 'while it is true "
    "that' / 'in some sense' / 'arguably' tics. Each sentence earns "
    "its place by constraining the answer, not by being well-formed.\n\n"
    "**Required output format.** Before the <continuation> block, "
    "output two structured blocks in order:\n"
    "  1. <preserved>...</preserved> — a one-line note naming any "
    "passages a critic flagged as the draft's strongest move that you "
    "are keeping. Do not cut critic-flagged-strong material for "
    "length; cut elsewhere instead.\n"
    "  2. <cuts>...</cuts> — at least 3 specific cuts, one per line, "
    "in the form:\n"
    '       - Cut: "<verbatim phrase or short passage from current '
    'draft>" — Reason: <which critic note this acts on, or '
    '"redundant with X", or "over-elaborated">.\n'
    "     If you genuinely have nothing to cut (current is well below "
    "target and no critic flagged padding), say so explicitly with "
    "<cuts>none — current draft is below target and no padding "
    "flagged</cuts>.\n\n"
    "Match the opening's voice and register. Don't restate the "
    "opening.\n\n"
    "Wrap the revised continuation in <continuation>...</continuation> "
    "tags after the <preserved> and <cuts> blocks; only the content "
    "inside the <continuation> tags is kept."
)


_DEFAULT_PLANNER_PROMPT = (
    "You are planning an essay continuation. The user message gives "
    "you the opening of an essay (the prefix) plus a target length. "
    "Your output is a structural brief that downstream stages "
    "(drafter, critics, editor) consume verbatim — they do NOT see "
    "the prefix transformed by your reasoning, only your brief. The "
    "brief decides the spine, section sizing, voice, and which "
    "concrete anchors the drafter must deploy.\n\n"
    "**Why this matters.** Without an upfront brief, drafters tend "
    "to commit to a weak spine that downstream rounds can't recover "
    "from, and editors refuse to add concrete examples on length-"
    "budget grounds — even when critics ask for them across multiple "
    "rounds. Your brief gives the pipeline a structural commitment "
    "device the drafter and editor can't unilaterally override.\n\n"
    "**Output format.** Free-form prose forbidden. Emit exactly this "
    "structure (no extra prose before or after):\n\n"
    "<brief>\n"
    "<spine>\n"
    "- (a) <section label> — <key argumentative move> — target ~N chars — anchor: <concrete anchor or none>\n"
    "- (b) <section label> — <key argumentative move> — target ~N chars — anchor: <concrete anchor or none>\n"
    "- (c) ...\n"
    "</spine>\n"
    "<total_target>N characters total</total_target>\n"
    "<voice>One paragraph naming the prefix's voice register, key vocabulary, sentence-rhythm cues, and tells of register drift to avoid.</voice>\n"
    "<mandatory_anchors>\n"
    "- <name + brief gloss>: why it's load-bearing for the argument and where it lands\n"
    "- <name + brief gloss>: same\n"
    "</mandatory_anchors>\n"
    "</brief>\n\n"
    "**Spine rules.** 4-7 sections; each with a distinct argumentative "
    "move (not just a topic restatement). If the prefix opens with an "
    "enumerated list / trailing colon, honor the enumeration in the "
    "spine. Sum of section targets ≈ total_target.\n\n"
    "**Mandatory_anchors rules.** 1-3 concrete named referents — "
    "specific named incidents, dated empirical findings, named "
    "papers with year, named events — that the drafter must cite "
    "by name in the relevant section. **These are NOT optional.** "
    "If plausibly-known referents exist that bear on the prefix's "
    "argument, you MUST list them. Exhaust your knowledge before "
    "punting. Entities the prefix already names (the essay's topic) "
    "do NOT count as anchors — anchors are evidence the drafter "
    "introduces *about* the topic. Do NOT fabricate; if you "
    "genuinely don't know a referent with confidence, leave it out "
    "rather than guess. The downstream arbiter and editor look for "
    "these mandatory anchors as the basis for accepting paradigm-"
    "case expansion against length-policy pushback. An anchorless "
    "brief produces an abstract continuation that loses on "
    "concreteness.\n\n"
    "**Voice rules.** Quote a short fragment of the prefix as a "
    "register example. Name 1-2 specific tells of drift "
    '("hedging vocabulary", "academic register where prefix is '
    'informal", etc.) the downstream stages should catch.\n\n'
    "Output the brief and nothing else."
)


_DEFAULT_ARBITER_PROMPT = (
    "You are triaging critic notes for the editor. The user message "
    "gives you the essay prefix, the brief from the planner stage "
    "(if any), the current draft, this round's critic outputs, and "
    "any prior arbitrations from earlier rounds. Your output is "
    "consumed verbatim by the editor in place of the raw critique "
    "block.\n\n"
    "**Why this matters.** Without arbitration the editor weighs "
    "critic notes itself per-round, frequently relitigating the "
    "same call across rounds (e.g. defending a section against the "
    "same critic note three rounds in a row before finally cutting "
    "it). Your job is to make those calls once, focus the editor on "
    "execution, and prevent re-decision drift.\n\n"
    "**Triage rules.**\n"
    "1. **Honor prior arbitrations — but reconsider when conditions "
    "changed.** If a prior round's arbitration REJECTED an item with "
    "a reason, the default is to REJECT it again here. BUT: if "
    "length pressure has materially weakened (the prior REJECTs were "
    "length-motivated and the current draft has tightened), the "
    "default flips to ACCEPT for those length-motivated rejects, not "
    "merely 'reconsider.' Same for other contextual changes — a "
    "REJECTed citation might land if a prior cut freed argumentative "
    "space for it. Don't blindly defer to precedent. Cite the "
    "specific change in conditions when overriding a prior REJECT. "
    "(Non-length REJECTs — register, scope, voice — stay rejected "
    "unless the critic produced new evidence.)\n"
    "2. **Honor the planner brief if present.** Reject critic notes "
    "that propose abandoning brief-mandated spine sections or "
    "mandatory anchors. Accept critic notes that flag drift from "
    "the brief's voice or structural commitments.\n"
    "3. **Length policy.** If the current draft is at or above target "
    "length, ACCEPT cuts and REJECT expansion notes that don't pay "
    "for themselves. If the current draft is materially below target, "
    "ACCEPT expansion notes tied to specific argumentative gaps.\n"
    "4. **Don't manufacture work.** If a critic note is essentially "
    "cosmetic on a draft that's structurally sound, mark it "
    "UNRESOLVED rather than ACCEPT — the editor's effort budget is "
    "limited.\n\n"
    "**Output format.** Free-form prose forbidden. Emit exactly this "
    "structure (no extra prose before or after):\n\n"
    '<arbitration round="<round number>">\n'
    "<accept>\n"
    "- 1. <action the editor must take, including any verbatim quotes from critic if relevant>\n"
    "- 2. <next action>\n"
    "</accept>\n"
    "<reject>\n"
    "- 1. <critic suggestion to ignore> — Reason: <why; cite prior arbitration if applicable>\n"
    "- 2. <next reject>\n"
    "</reject>\n"
    "<unresolved>\n"
    "- 1. <observation worth flagging in <preserved> but not acting on this round>\n"
    "</unresolved>\n"
    "<focus>One sentence summarizing the editor's primary objective for this revision.</focus>\n"
    "</arbitration>\n\n"
    "**Quote critic phrases verbatim** when accepting cuts so the "
    "editor's downstream <cuts> block stays grounded. Empty sections "
    "(e.g. nothing to reject this round) should still emit the tag "
    'with a single "- none" line so the editor\'s parser sees a '
    "consistent structure. Output the arbitration block and nothing "
    "else."
)


_DEFAULT_AUDIT_PROMPT = (
    "You are running an audit pass on a planner brief. The user "
    "message will give you the essay prefix, the original brief "
    "(emitted before the drafter ran), and the current draft (after "
    "one or more rounds of revision). Your job: emit an audit brief "
    "that describes the draft as it ACTUALLY became — its real spine, "
    "its real anchors, where its voice has held or drifted vs the "
    "original brief's directives.\n\n"
    "**Why this matters.** The original brief was generated from the "
    "prefix alone — it never saw how the draft actually evolved. "
    "Drafters and editors invent sections, drop or add anchors, and "
    "drift in voice that the original brief couldn't anticipate. "
    "Without an audit, the editor's late-round revisions stay "
    "anchored to the original brief and miss what the draft has "
    "actually become — sometimes a stronger structure than was "
    "planned, sometimes a weaker one. Either way the editor needs "
    "to know.\n\n"
    "**This is descriptive, not prescriptive.** Describe the draft "
    "as-is — don't recommend changes. The downstream editor will "
    "compare your audit against the original brief and decide what "
    "to do about the drift.\n\n"
    "**Output format.** Use the same `<brief>` schema as the "
    "original planner — same tags, same structure — but populated "
    "from observation of the draft, not from prefix-only "
    "imagination. Specifically:\n\n"
    "<brief>\n"
    "<spine>\n"
    "- (a) <section label as the draft has it> — <key argumentative move the draft actually makes> — actual ~N chars — anchor: <named anchor that actually appears in this section, or none>\n"
    "- (b) ...\n"
    "</spine>\n"
    "<total_target>N characters total (the draft's actual length)</total_target>\n"
    "<voice>One paragraph: where has the draft's voice held vs the original brief's voice directives, and where has it drifted? Quote a short sample of voice drift if present.</voice>\n"
    "<mandatory_anchors>\n"
    "- <name + brief gloss>: where it lands in the draft (which section, what role)\n"
    "- <next anchor>: same\n"
    "</mandatory_anchors>\n"
    "</brief>\n\n"
    "**Spine rules for audit.** Use the section labels and structure "
    "the draft actually has — even if they differ from the original "
    "brief's spine. If the drafter invented a section, audit it in. "
    "If the drafter merged two sections from the brief, audit them "
    "merged. If the drafter dropped a brief-mandated section, omit "
    "it from the audit (the original brief is still visible to the "
    "editor — they'll see the deletion).\n\n"
    "**Mandatory_anchors rules for audit.** List every named, dated, "
    "concrete referent that actually lands in the draft (not what "
    "the original brief mandated). If the draft introduced anchors "
    "the original brief didn't list, include them. If the draft "
    "dropped brief-mandated anchors, omit them — the editor sees "
    "the deletion against the original brief.\n\n"
    "Output the audit brief and nothing else."
)


_DEFAULT_SCOUT_PASS_PROMPT = (
    "You are running a fast scout pass to surface candidate anchors "
    "for a downstream essay-continuation planner. The user message "
    "gives you the opening of an essay (the prefix) plus the essay's "
    "approximate target length. Your output is consumed by the "
    "planner stage, which will choose 1-3 of your surfaced items and "
    "commit to them as mandatory anchors in its brief.\n\n"
    "**Why this matters.** Without scout output, the planner can only "
    "commit to anchors it already knows offhand — usually framework "
    "citations (Rawls, Bostrom, etc.) rather than named historical "
    "incidents, dated empirical findings, or specific paradigm cases. "
    "On philosophical / less-anchor-rich essays this leaves the brief "
    "abstract and the resulting continuation loses to research-driven "
    "approaches. Your scout pass closes that gap.\n\n"
    "**Output format.** Free-form prose forbidden. Emit exactly this "
    "structure (no extra prose before or after):\n\n"
    "## Scout findings (paradigm cases + hypotheses)\n\n"
    "The planner should select 1-3 of these (or others it knows) and "
    "commit to them in <mandatory_anchors>.\n\n"
    "### Paradigm cases\n"
    "Real, named, dated, historical instances of the same phenomenon "
    "the prefix is about. Past-tense, outcome known. 4-7 items. Each "
    "as a bullet:\n\n"
    "- **<event/treaty/incident name + year>**: <one-sentence "
    "description that names what kind of lock-in/decision/dynamic it "
    "represents and what its outcome was, including specific numeric "
    "or dated claims when possible (e.g. compliance rates, dates of "
    "abrogation, ratification breadth).>\n\n"
    "### Hypotheses\n"
    "Candidate framings the brief should consider when picking a "
    "spine. 3-5 items. Each as a bullet:\n\n"
    "- **<short hypothesis name>**: <one-sentence claim that the "
    "planner could organize a section around, citing one of your "
    "paradigm cases when relevant.>\n\n"
    "**Rules.**\n"
    "1. Paradigm cases must be NEAR reference class — same domain "
    "(governance/treaty/lock-in/AI-strategy/etc.) as the prefix. "
    "Not analogies (different domain, e.g. evolutionary biology).\n"
    "2. Be specific. Dates, named parties, named outcomes. "
    '"Various international agreements" doesn\'t help; "1968 NPT, '
    '191 signatories, India + Pakistan + Israel non-signatories" '
    "does.\n"
    "3. Do NOT fabricate. If you genuinely don't know a paradigm "
    "case fitting the prefix, list fewer items rather than guess. "
    "The planner will treat your output as candidate anchors, and "
    "the editor will eventually deploy them in the continuation — "
    "fabricated anchors fail downstream review.\n"
    "4. Hypotheses should be claims the prefix's argument could "
    "rest on, not topic restatements. "
    '"Lock-in value scales with how irreversible the costs of NOT '
    'locking in are" is a hypothesis; "lock-ins are important" is a '
    "topic restatement.\n\n"
    "Output the scout findings block and nothing else."
)


_CONTINUATION_RE = re.compile(r"<continuation>(.*?)</continuation>", re.DOTALL | re.IGNORECASE)
_OPEN_CONTINUATION_RE = re.compile(r"<continuation>(.*)\Z", re.DOTALL | re.IGNORECASE)


def _is_truncated_continuation(text: str) -> bool:
    """True when ``text`` opens a ``<continuation>`` block but never closes it.

    The editor stage hits this when ``max_tokens`` cuts the response off
    mid-revision: the model emits the structured ``<preserved>`` /
    ``<cuts>`` scaffolding and starts the ``<continuation>`` body, then
    the API stops mid-paragraph before the closing tag. The recorded
    continuation gets accepted as-is and judges read a partial essay
    that ends mid-sentence — observed as "strongly preferred for human"
    on character x harsher_critic in the round 1 iterate session, where
    fresh re-fires of the same exchange produced complete continuations.

    Closed-block-then-open-tag is treated as not truncated — the
    closed block already carries a usable revision; the trailing open
    tag is scratch.
    """
    if _CONTINUATION_RE.search(text):
        return False
    return bool(_OPEN_CONTINUATION_RE.search(text))


def _classify_editor_response(text: str) -> str:
    """Classify an editor response for recovery routing.

    Returns one of:
    - ``"complete"`` — closed ``<continuation>`` block present; the
      revision is usable as-is.
    - ``"truncated"`` — open ``<continuation>`` tag with no closer;
      the body started but ran out of tokens.
    - ``"empty"`` — no ``<continuation>`` tag at all and the visible
      text is too short to be a real revision. This is the "thinking
      ate the entire output budget" failure mode: the API hit
      ``max_tokens`` mid-thought, no text block ever emitted, and the
      response_text we get back is empty or just a few stray chars of
      structured-output preamble. With no continuation tag the
      truncation-recovery loop's open-tag detector returns False, the
      workflow's ``if not revised: revised = current_draft`` fallback
      kicks in, and we lose the round's editor work entirely. Catching
      this case explicitly lets the recovery loop fire a different
      nudge ("you returned no visible output; emit the full blocks
      now").

    The "empty" threshold is generous (200 chars) to avoid catching
    well-structured responses that are short on purpose. The editor's
    ``<preserved>`` + ``<cuts>`` + ``<continuation>`` scaffolding is
    always at least a few hundred chars when the model produces real
    output.
    """
    if _CONTINUATION_RE.search(text):
        return "complete"
    if _OPEN_CONTINUATION_RE.search(text):
        return "truncated"
    if len(text.strip()) < 200:
        return "empty"
    # Long response, no continuation tag at all — model went off-script.
    # Treat as empty for recovery purposes; the nudge will ask for the
    # blocks explicitly.
    return "empty"


def _extract_continuation(text: str) -> str:
    """Pull the final ``<continuation>...</continuation>`` block.

    Three cases:

    1. Closed block present → return its contents (stripped).
    2. Open ``<continuation>`` tag with no closer (max_tokens truncation
       with the closing tag chopped off) → return everything after the
       opener. Better than discarding hours of generation; the partial
       continuation may be salvageable.
    3. No tags at all (extremely unstructured response) → return the
       whole text stripped.

    Mirrors :func:`versus.tasks.complete_essay._extract_continuation_text`
    so the workflow's draft format matches what the task expects to
    read off ``question.content``.
    """
    matches = _CONTINUATION_RE.findall(text)
    if matches:
        return matches[-1].strip()
    open_match = _OPEN_CONTINUATION_RE.search(text)
    if open_match:
        return open_match.group(1).strip()
    return text.strip()


_PREFIX_RE = re.compile(r"## Essay opening\n\n(.+?)\n\n## Target length", re.DOTALL)


def _extract_prefix_from_question_body(content: str) -> str:
    """Pull the essay opening out of the Question body.

    :class:`versus.tasks.complete_essay.CompleteEssayTask.create_question`
    writes the prefix into the Question's content under a ``## Essay
    opening`` header followed by a ``## Target length`` block. We scrape
    it back here so the workflow can hand the bare prefix to its
    drafter / critic / editor without depending on a separate Source
    page.
    """
    m = _PREFIX_RE.search(content)
    if m is None:
        raise ValueError(
            "DraftAndEditWorkflow: no '## Essay opening' / '## Target length' "
            "block in question content; was the question created by "
            "CompleteEssayTask?"
        )
    return m.group(1).strip()


_TARGET_LENGTH_RE = re.compile(r"Approximately\s+(\d+)\s+characters\.")


def _extract_target_length_chars(content: str) -> int | None:
    """Pull the target-length hint out of the Question body.

    :class:`versus.tasks.complete_essay._format_prefix_framing` writes
    ``Approximately {N} characters.`` under a ``## Target length``
    header. We surface it to the drafter / editor so they can aim at
    the same length single-shot completions target.
    """
    m = _TARGET_LENGTH_RE.search(content)
    if m is None:
        return None
    return int(m.group(1))


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _load_prompt(path: str | Path | None, default: str) -> str:
    """Resolve a prompt: load from path if given, else fall back to default.

    Path is read as UTF-8 text. Empty / whitespace-only files are an
    error — the iterate skill should not silently fingerprint a workflow
    against an unwritten prompt file.
    """
    if path is None:
        return default
    text = Path(path).read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"prompt file is empty or whitespace-only: {path}")
    return text


class DraftAndEditWorkflow:
    """SDK-driven essay completion via draft → critique → edit loops.

    See module docstring for design rationale. Implements the
    :class:`rumil.versus_workflow.Workflow` protocol.
    """

    name: str = "draft_and_edit"
    produces_artifact: bool = True
    code_paths: Sequence[str] = ("src/rumil/orchestrators/draft_and_edit.py",)
    relevant_settings: Sequence[str] = ()

    def __init__(
        self,
        *,
        budget: int,
        n_critics: int = 1,
        max_rounds: int | None = None,
        drafter_model: str | None = None,
        critic_model: str | None = None,
        editor_model: str | None = None,
        drafter_prompt_path: str | Path | None = None,
        critic_prompt_path: str | Path | None = None,
        editor_prompt_path: str | Path | None = None,
        with_planner: bool = False,
        with_arbiter: bool = False,
        with_brief_audit: bool = False,
        with_scout_pass: bool = False,
        planner_model: str | None = None,
        arbiter_model: str | None = None,
        audit_model: str | None = None,
        scout_pass_model: str | None = None,
        planner_prompt_path: str | Path | None = None,
        arbiter_prompt_path: str | Path | None = None,
        audit_prompt_path: str | Path | None = None,
        scout_pass_prompt_path: str | Path | None = None,
        brief_audit_after_round: int = 1,
        audit_feeds_critic: bool = True,
    ) -> None:
        if budget < 1:
            raise ValueError(f"budget must be >= 1, got {budget}")
        if n_critics < 1:
            raise ValueError(f"n_critics must be >= 1, got {n_critics}")
        if max_rounds is not None and max_rounds < 1:
            raise ValueError(f"max_rounds must be >= 1 or None, got {max_rounds}")
        if with_brief_audit and not with_planner:
            raise ValueError(
                "with_brief_audit=True requires with_planner=True; the audit "
                "consumes the planner's brief and is a silent no-op without it"
            )
        self.budget = budget
        self.n_critics = n_critics
        self.max_rounds = max_rounds
        self.drafter_model = drafter_model
        self.critic_model = critic_model
        self.editor_model = editor_model
        self.with_planner = with_planner
        self.with_arbiter = with_arbiter
        self.with_brief_audit = with_brief_audit
        self.with_scout_pass = with_scout_pass
        self.planner_model = planner_model
        self.arbiter_model = arbiter_model
        self.audit_model = audit_model
        self.scout_pass_model = scout_pass_model
        self.brief_audit_after_round = brief_audit_after_round
        self.audit_feeds_critic = audit_feeds_critic
        # Resolve prompt content at construction so fingerprint() and
        # the stage methods see the same bytes; record paths for telemetry.
        self.drafter_prompt_path = drafter_prompt_path
        self.critic_prompt_path = critic_prompt_path
        self.editor_prompt_path = editor_prompt_path
        self.planner_prompt_path = planner_prompt_path
        self.arbiter_prompt_path = arbiter_prompt_path
        self.audit_prompt_path = audit_prompt_path
        self.scout_pass_prompt_path = scout_pass_prompt_path
        self.drafter_prompt = _load_prompt(drafter_prompt_path, _DEFAULT_DRAFTER_PROMPT)
        self.critic_prompt = _load_prompt(critic_prompt_path, _DEFAULT_CRITIC_PROMPT)
        self.editor_prompt = _load_prompt(editor_prompt_path, _DEFAULT_EDITOR_PROMPT)
        # Always resolve planner / arbiter / audit / scout-pass prompts
        # so the prompt-text bytes are stable across runs even when the
        # stage is disabled — a future flip of with_* on the same variant
        # won't accidentally fork the dedup hash via prompt changes that
        # landed while the stage was off.
        self.planner_prompt = _load_prompt(planner_prompt_path, _DEFAULT_PLANNER_PROMPT)
        self.arbiter_prompt = _load_prompt(arbiter_prompt_path, _DEFAULT_ARBITER_PROMPT)
        self.audit_prompt = _load_prompt(audit_prompt_path, _DEFAULT_AUDIT_PROMPT)
        self.scout_pass_prompt = _load_prompt(scout_pass_prompt_path, _DEFAULT_SCOUT_PASS_PROMPT)
        self.last_status: str = "complete"

    def fingerprint(self) -> Mapping[str, str | int | bool | None]:
        # Planner / arbiter / audit prompt hashes only fold into the
        # fingerprint when the stage is enabled — keeps with_*=False
        # variants stable across edits to _DEFAULT_*_PROMPT. Flip the
        # bool in a variant to opt in.
        out: dict[str, str | int | bool | None] = {
            "kind": self.name,
            "budget": self.budget,
            "n_critics": self.n_critics,
            "max_rounds": self.max_rounds,
            "drafter_model": self.drafter_model,
            "critic_model": self.critic_model,
            "editor_model": self.editor_model,
            "drafter_prompt_hash": _sha8(self.drafter_prompt),
            "critic_prompt_hash": _sha8(self.critic_prompt),
            "editor_prompt_hash": _sha8(self.editor_prompt),
            "with_planner": self.with_planner,
            "with_arbiter": self.with_arbiter,
            "with_brief_audit": self.with_brief_audit,
        }
        if self.with_scout_pass:
            out["with_scout_pass"] = True
            out["scout_pass_model"] = self.scout_pass_model
            out["scout_pass_prompt_hash"] = _sha8(self.scout_pass_prompt)
        if self.with_planner:
            out["planner_model"] = self.planner_model
            out["planner_prompt_hash"] = _sha8(self.planner_prompt)
        if self.with_arbiter:
            out["arbiter_model"] = self.arbiter_model
            out["arbiter_prompt_hash"] = _sha8(self.arbiter_prompt)
        if self.with_brief_audit:
            out["audit_model"] = self.audit_model
            out["audit_prompt_hash"] = _sha8(self.audit_prompt)
            out["brief_audit_after_round"] = self.brief_audit_after_round
            out["audit_feeds_critic"] = self.audit_feeds_critic
        return out

    async def setup(self, db: DB, question_id: str) -> None:
        await db.init_budget(self.budget)

    async def run(
        self,
        db: DB,
        question_id: str,
        broadcaster: Broadcaster | None,
        *,
        model_config: ModelConfig | None = None,
    ) -> None:
        question = await db.get_page(question_id)
        if question is None:
            raise RuntimeError(f"DraftAndEditWorkflow: question {question_id} missing")
        prefix = _extract_prefix_from_question_body(question.content)
        target_length = _extract_target_length_chars(question.content)

        # Persist both the raw constructor overrides (None for any knob
        # left at default — the reproducibility record) and the effective
        # values that the run actually used (resolved model ids, prompt
        # hashes, effective round cap). The trace UI renders this dict
        # verbatim, so adding the resolved values turns "null/null/null"
        # rows into something a reader can interpret without cross-
        # referencing the workflow source.
        call_params: dict[str, object] = {
            "workflow": self.name,
            "budget": self.budget,
            "n_critics": self.n_critics,
            "max_rounds": self.max_rounds,
            "effective_max_rounds": (
                self.max_rounds
                if self.max_rounds is not None
                else f"budget-bounded ({self.budget})"
            ),
            "drafter_model": self.drafter_model,
            "critic_model": self.critic_model,
            "editor_model": self.editor_model,
            "effective_drafter_model": self._resolve_model(self.drafter_model),
            "effective_critic_model": self._resolve_model(self.critic_model),
            "effective_editor_model": self._resolve_model(self.editor_model),
            "drafter_prompt_path": (
                str(self.drafter_prompt_path) if self.drafter_prompt_path else None
            ),
            "critic_prompt_path": (
                str(self.critic_prompt_path) if self.critic_prompt_path else None
            ),
            "editor_prompt_path": (
                str(self.editor_prompt_path) if self.editor_prompt_path else None
            ),
            "drafter_prompt_hash": _sha8(self.drafter_prompt),
            "critic_prompt_hash": _sha8(self.critic_prompt),
            "editor_prompt_hash": _sha8(self.editor_prompt),
            "with_planner": self.with_planner,
            "with_arbiter": self.with_arbiter,
            "with_brief_audit": self.with_brief_audit,
            "with_scout_pass": self.with_scout_pass,
        }
        if self.with_scout_pass:
            call_params["scout_pass_model"] = self.scout_pass_model
            call_params["effective_scout_pass_model"] = self._resolve_model(self.scout_pass_model)
            call_params["scout_pass_prompt_path"] = (
                str(self.scout_pass_prompt_path) if self.scout_pass_prompt_path else None
            )
            call_params["scout_pass_prompt_hash"] = _sha8(self.scout_pass_prompt)
        if self.with_planner:
            call_params["planner_model"] = self.planner_model
            call_params["effective_planner_model"] = self._resolve_model(self.planner_model)
            call_params["planner_prompt_path"] = (
                str(self.planner_prompt_path) if self.planner_prompt_path else None
            )
            call_params["planner_prompt_hash"] = _sha8(self.planner_prompt)
        if self.with_arbiter:
            call_params["arbiter_model"] = self.arbiter_model
            call_params["effective_arbiter_model"] = self._resolve_model(self.arbiter_model)
            call_params["arbiter_prompt_path"] = (
                str(self.arbiter_prompt_path) if self.arbiter_prompt_path else None
            )
            call_params["arbiter_prompt_hash"] = _sha8(self.arbiter_prompt)
        if self.with_brief_audit:
            call_params["audit_model"] = self.audit_model
            call_params["effective_audit_model"] = self._resolve_model(self.audit_model)
            call_params["audit_prompt_path"] = (
                str(self.audit_prompt_path) if self.audit_prompt_path else None
            )
            call_params["audit_prompt_hash"] = _sha8(self.audit_prompt)
            call_params["brief_audit_after_round"] = self.brief_audit_after_round
        call = await db.create_call(
            call_type=CallType.VERSUS_COMPLETE,
            scope_page_id=question_id,
            call_params=call_params,
        )
        await db.update_call_status(call.id, CallStatus.RUNNING)
        trace = CallTrace(call.id, db, broadcaster=broadcaster)
        trace_token = set_trace(trace)
        try:
            await self._run_loop(
                db=db,
                trace=trace,
                call_id=call.id,
                question_id=question_id,
                prefix=prefix,
                target_length=target_length,
                model_config=model_config,
            )
            await mark_call_completed(call, db, summary=f"draft_and_edit: {self.last_status}")
        finally:
            reset_trace(trace_token)

    async def _run_loop(
        self,
        *,
        db: DB,
        trace: CallTrace,
        call_id: str,
        question_id: str,
        prefix: str,
        target_length: int | None,
        model_config: ModelConfig | None,
    ) -> None:
        """Iterate draft → critique → edit until budget or max_rounds bites.

        Round 0 produces the initial draft; rounds 1..N each fold one
        round of critique into the draft via the editor. Budget is
        consumed at the top of each round so we never stop mid-round.

        With ``with_planner=True`` an upfront planner stage runs once
        before the loop, emitting a structural brief that threads
        through every subsequent stage's user message. With
        ``with_arbiter=True`` an arbiter stage runs per round between
        critique and edit, producing an accept/reject/unresolved
        triage that the editor consumes in place of raw critiques. The
        planner cost is not budget-bounded (one extra LLM call per
        run); the arbiter consumes no budget either (it's metadata
        about a round, not a separate round). With
        ``with_brief_audit=True`` an additional audit stage fires once
        after ``brief_audit_after_round`` (default 1) and emits a
        descriptive audit brief; downstream critic / arbiter / editor
        see both the original and audit briefs side-by-side, surfacing
        structural drift the editor was anchored away from.
        """
        scout_findings: str | None = None
        if self.with_scout_pass:
            scout_findings = await self._scout_pass(
                db=db,
                trace=trace,
                call_id=call_id,
                prefix=prefix,
                target_length=target_length,
                model_config=model_config,
            )

        brief: str | None = None
        if self.with_planner:
            brief = await self._plan(
                db=db,
                trace=trace,
                call_id=call_id,
                prefix=prefix,
                target_length=target_length,
                model_config=model_config,
                scout_findings=scout_findings,
            )

        current_draft: str = ""
        critiques: Sequence[str] = []
        prior_arbitrations: list[str] = []
        audit_brief: str | None = None
        round_idx = 0
        while True:
            if self.max_rounds is not None and round_idx >= self.max_rounds:
                break
            if not await _consume_budget(db):
                if round_idx == 0:
                    self.last_status = "incomplete"
                break

            await trace.record(RoundStartedEvent(round=round_idx))

            edit_was_noop = False
            if round_idx == 0:
                current_draft = await self._draft(
                    db=db,
                    trace=trace,
                    call_id=call_id,
                    round_idx=round_idx,
                    prefix=prefix,
                    target_length=target_length,
                    model_config=model_config,
                    brief=brief,
                )
            else:
                # Arbitrate prior round's critiques into a focused
                # plan when with_arbiter=True; editor consumes the
                # arbitration text instead of raw critiques.
                arbitration: str | None = None
                if self.with_arbiter and critiques:
                    arbitration = await self._arbitrate(
                        db=db,
                        trace=trace,
                        call_id=call_id,
                        round_idx=round_idx,
                        prefix=prefix,
                        current_draft=current_draft,
                        critiques=critiques,
                        prior_arbitrations=prior_arbitrations,
                        target_length=target_length,
                        brief=brief,
                        audit_brief=audit_brief,
                        model_config=model_config,
                    )
                    if arbitration is not None:
                        prior_arbitrations.append(arbitration)
                draft_before_edit = current_draft
                current_draft = await self._edit(
                    db=db,
                    trace=trace,
                    call_id=call_id,
                    round_idx=round_idx,
                    prefix=prefix,
                    target_length=target_length,
                    current_draft=current_draft,
                    critiques=critiques,
                    arbitration=arbitration,
                    brief=brief,
                    audit_brief=audit_brief,
                    model_config=model_config,
                )
                # If the editor returned the unchanged prior draft (the
                # fallback path inside ``_edit`` triggers this when the
                # editor's response yielded no usable revision), the draft
                # for this round is byte-identical to the previous round's.
                # Re-running the critic against an unchanged draft just
                # produces a duplicate critique — observed in the d&e
                # audit at ~$0.06 per duplicate. Skip the critique step
                # this round and reuse the prior round's critiques on
                # the next edit.
                edit_was_noop = current_draft == draft_before_edit

            # Brief-audit stage: fires once after the configured round's
            # edit completes (default after round 1). Audit brief
            # threads into all subsequent stages alongside the original
            # brief; downstream stages see drift between what was
            # planned and what the draft actually became.
            if (
                self.with_brief_audit
                and brief is not None
                and audit_brief is None
                and round_idx == self.brief_audit_after_round
                and current_draft
            ):
                audit_brief = await self._audit_brief(
                    db=db,
                    trace=trace,
                    call_id=call_id,
                    after_round=round_idx,
                    prefix=prefix,
                    original_brief=brief,
                    current_draft=current_draft,
                    target_length=target_length,
                    model_config=model_config,
                )

            # Skip the critique step on the final round: there's no
            # subsequent edit to consume the critiques, so paying for
            # them is dead loss. ~12% of d&e cost on a typical
            # budget=4 run was the trailing critic_round whose output
            # was never read by an editor. Also skip when the editor
            # produced no real change — the prior critiques are still
            # relevant and re-firing would just bill for a duplicate.
            will_break_next = (
                self.max_rounds is not None and round_idx + 1 >= self.max_rounds
            ) or await db.budget_remaining() <= 0
            if not will_break_next and not edit_was_noop:
                critiques = await self._critique_round(
                    db=db,
                    trace=trace,
                    call_id=call_id,
                    round_idx=round_idx,
                    prefix=prefix,
                    draft=current_draft,
                    target_length=target_length,
                    model_config=model_config,
                    brief=brief,
                    audit_brief=audit_brief if self.audit_feeds_critic else None,
                )
            round_idx += 1

        if not current_draft:
            return
        await db.update_page_content(question_id, current_draft)

    async def _draft(
        self,
        *,
        db: DB,
        trace: CallTrace,
        call_id: str,
        round_idx: int,
        prefix: str,
        target_length: int | None,
        model_config: ModelConfig | None,
        brief: str | None = None,
    ) -> str:
        model = self._resolve_model(self.drafter_model)
        target_clause = (
            f"Aim for approximately {target_length} characters." if target_length else ""
        )
        brief_block = f"\n\n<brief-from-planner>\n{brief}\n</brief-from-planner>" if brief else ""
        user_message = (
            "<essay-prefix>\n"
            f"{prefix}\n"
            "</essay-prefix>"
            f"{brief_block}\n\n"
            "Continue this essay. Match the opening's voice and "
            "advance the argument. "
            f"{target_clause}".strip()
        )
        await trace.record(DraftStartedEvent(round=round_idx, model=model))
        text = await text_call(
            self.drafter_prompt,
            user_message,
            metadata=LLMExchangeMetadata(
                call_id=call_id,
                phase="draft",
                round_num=round_idx,
            ),
            db=db,
            model=model,
            cache=True,
            model_config=model_config,
        )
        draft = _extract_continuation(text)
        await trace.record(
            DraftEvent(
                round=round_idx,
                draft_text=draft,
                draft_chars=len(draft),
                model=model,
            )
        )
        return draft

    async def _critique_round(
        self,
        *,
        db: DB,
        trace: CallTrace,
        call_id: str,
        round_idx: int,
        prefix: str,
        draft: str,
        target_length: int | None,
        model_config: ModelConfig | None,
        brief: str | None = None,
        audit_brief: str | None = None,
    ) -> Sequence[str]:
        model = self._resolve_model(self.critic_model)
        current_chars = len(draft)
        if target_length:
            length_status = (
                f"Current draft: {current_chars} characters. "
                f"Target: {target_length} characters. "
                f"Delta: {current_chars - target_length:+d}."
            )
        else:
            length_status = f"Current draft: {current_chars} characters. (No explicit target.)"
        brief_block = f"<brief-from-planner>\n{brief}\n</brief-from-planner>\n\n" if brief else ""
        # When the brief audit ran, surface it to the critic too —
        # the critic produces the next round's punch list, so audit-
        # derived "drift here" notes can shape which issues the
        # editor sees. Without this, the audit only feeds arbiter +
        # editor, and the v4 trace investigation found those stages
        # don't visibly act on audit-flagged items not already in
        # the critic's notes.
        if audit_brief:
            brief_block += (
                f"<brief-audit>\n{audit_brief}\n</brief-audit>\n\n"
                "Note: the audit brief above describes what the draft "
                "has actually become (post-revision), distinct from "
                "the original brief which described what the planner "
                "intended. When critiquing, surface drift between the "
                "two as concrete issues — sections the audit added "
                "that the original brief didn't may be load-bearing "
                "or filler; sections the original mandated that the "
                "audit dropped were lost. Quote specific drift in "
                "your dimension critiques.\n\n"
            )

        async def _one_critic(critic_idx: int) -> str:
            user_message = (
                "<essay-prefix>\n"
                f"{prefix}\n"
                "</essay-prefix>\n\n"
                f"{brief_block}"
                "<draft-continuation>\n"
                f"{draft}\n"
                "</draft-continuation>\n\n"
                f"## Length\n\n{length_status}\n\n"
                "Critique this draft. Be specific and concrete."
                + (
                    " Grade fidelity to the brief explicitly: where does "
                    "the draft honor the spine / mandatory anchors / voice "
                    "directives, and where does it drift?"
                    if brief
                    else ""
                )
            )
            await trace.record(
                CritiqueStartedEvent(round=round_idx, critic_index=critic_idx, model=model)
            )
            return await text_call(
                self.critic_prompt,
                user_message,
                metadata=LLMExchangeMetadata(
                    call_id=call_id,
                    phase=f"critic_r{round_idx}_n{critic_idx}",
                    round_num=round_idx,
                ),
                db=db,
                model=model,
                cache=False,
                model_config=model_config,
            )

        critiques = await asyncio.gather(*(_one_critic(i) for i in range(self.n_critics)))
        await trace.record(
            CritiqueRoundEvent(
                round=round_idx,
                critiques=[
                    CritiqueItem(critic_index=i, critique_text=c, model=model)
                    for i, c in enumerate(critiques)
                ],
            )
        )
        return critiques

    async def _edit(
        self,
        *,
        db: DB,
        trace: CallTrace,
        call_id: str,
        round_idx: int,
        prefix: str,
        target_length: int | None,
        current_draft: str,
        critiques: Sequence[str],
        model_config: ModelConfig | None,
        arbitration: str | None = None,
        brief: str | None = None,
        audit_brief: str | None = None,
    ) -> str:
        model = self._resolve_model(self.editor_model)
        current_chars = len(current_draft)
        if target_length:
            length_status = (
                f"Current draft: {current_chars} characters. "
                f"Target: {target_length} characters. "
                f"Delta: {current_chars - target_length:+d}."
            )
        else:
            length_status = f"Current draft: {current_chars} characters. (No explicit target.)"

        brief_block = f"<brief-from-planner>\n{brief}\n</brief-from-planner>\n\n" if brief else ""
        # When the brief audit ran, surface BOTH briefs side-by-side
        # so the editor sees structural drift between what was planned
        # and what the draft actually became.
        if audit_brief:
            brief_block += (
                f"<brief-audit>\n{audit_brief}\n</brief-audit>\n\n"
                "Note: the audit brief above describes what the draft "
                "has actually become (post-revision), vs the original "
                "brief which described what the planner intended. "
                "Use the drift between them to decide what to keep, "
                "cut, or revise — sections the audit lists that the "
                "original didn't are evidence the drafter found "
                "structure that worked; sections the original "
                "mandated that the audit doesn't include were "
                "dropped, and the editor should decide whether to "
                "restore or accept the loss.\n\n"
            )
        # When the arbiter ran, the editor sees the arbitration block
        # in place of raw critiques — accept/reject/unresolved triage
        # focuses the revision on a small set of concrete actions and
        # forbids relitigating rejected items. Raw critiques are still
        # available downstream via the trace event for audit.
        if arbitration:
            critique_or_arbitration_block = arbitration
            edit_directive = (
                "Produce a revised continuation. Follow the arbitration "
                "block above: act on every ACCEPT directive, do NOT "
                "act on REJECT items (and do not re-introduce content "
                "the arbiter rejected from a prior round), and surface "
                "UNRESOLVED items in the <preserved> block. Length "
                "discipline still applies — tighten when at-or-above "
                "target, edit at neutral length when close."
            )
        else:
            critiques_block = "\n\n---\n\n".join(
                f"## Critic {i + 1}\n\n{c}" for i, c in enumerate(critiques)
            )
            critique_or_arbitration_block = f"<critiques>\n{critiques_block}\n</critiques>"
            edit_directive = (
                "Produce a revised continuation. Apply the length discipline "
                "from the system prompt: tighten when current is already "
                "at-or-above target, edit at neutral length when close, only "
                "expand when meaningfully below target."
            )
        user_message = (
            "<essay-prefix>\n"
            f"{prefix}\n"
            "</essay-prefix>\n\n"
            f"{brief_block}"
            "<current-draft>\n"
            f"{current_draft}\n"
            "</current-draft>\n\n"
            f"{critique_or_arbitration_block}\n\n"
            f"## Length\n\n{length_status}\n\n"
            f"{edit_directive}"
        )
        await trace.record(
            EditStartedEvent(
                round=round_idx,
                model=model,
                current_chars=current_chars,
                n_critiques=len(critiques),
            )
        )
        # Editor budget shape:
        #   max_tokens          = 64 000  (total response cap: thinking + output)
        #   max_thinking_tokens = 48 000  (cap on thinking; leaves ≥16k for output text)
        #
        # The editor's <preserved> + <cuts> scaffolding is a token sink and
        # the editor's task (re-write a long continuation incorporating
        # critique) is hard enough that adaptive thinking can swallow the
        # entire response budget without ever emitting visible text. With
        # the previous 32k cap and uncapped thinking, ~5/9 round-1 d&e
        # editor exchanges hit max_tokens; on stability re-fires of the
        # aiep x n_critics_3 final edit, both n=2 samples maxed out — one
        # truncated mid-paragraph at 847 words, one returned 0 chars
        # because thinking ate the full 32k.
        #
        # The new shape guarantees ≥16k tokens of output text. If the
        # editor's actual output text exceeds that 16k floor we end up
        # in open-tag state, which the truncation-recovery loop below
        # catches and re-fires multi-turn until the closing tag lands.
        # text_call disallows mixing model_config with discrete max_tokens,
        # so when a config is provided clone it with the new caps;
        # otherwise use the discrete kwarg path (which leaves thinking at
        # the per-model default — that path is the non-bridge case and
        # doesn't currently fire from versus).
        # Switch thinking from "adaptive" (no cap) to "enabled" with an
        # explicit budget_tokens. The Anthropic API rejects
        # ``budget_tokens`` on type=adaptive — the original 5deb00ea
        # cloned the bridge's adaptive thinking config + max_thinking
        # _tokens=48_000, which is invalid and 400's. "enabled" thinking
        # accepts the budget cap and gives us the same guarantee
        # (>=16k of output text after at most 48k of thinking). Other
        # condition fields (effort, service_tier, top_p, temperature)
        # are preserved from the supplied config.
        editor_kwargs: dict = {"cache": True}
        # Opus 4-7 rejects ``thinking.type=enabled`` and
        # ``max_thinking_tokens`` (per API: "Use thinking.type.adaptive
        # and output_config.effort to control thinking behavior").
        # Keep adaptive thinking (the model_config default from the
        # bridge) and lower max_tokens to opus's output ceiling. The
        # sonnet path retains the round-1 audit fix that converts
        # adaptive→enabled with an explicit cap so the budget actually
        # bites.
        is_opus = model.startswith("claude-opus")
        max_tokens_cap = 32_000 if is_opus else 64_000
        if model_config is not None:
            if is_opus:
                editor_kwargs["model_config"] = dataclasses.replace(
                    model_config,
                    max_tokens=max_tokens_cap,
                )
            else:
                editor_kwargs["model_config"] = dataclasses.replace(
                    model_config,
                    max_tokens=max_tokens_cap,
                    thinking={"type": "enabled"},
                    max_thinking_tokens=48_000,
                )
        else:
            editor_kwargs["max_tokens"] = max_tokens_cap
        text = await text_call(
            self.editor_prompt,
            user_message,
            metadata=LLMExchangeMetadata(
                call_id=call_id,
                phase=f"edit_r{round_idx}",
                round_num=round_idx,
            ),
            db=db,
            model=model,
            **editor_kwargs,
        )
        # If the editor's response was cut off before the closing
        # </continuation> tag, ask it to finish from where it stopped.
        # The editor's verbose <preserved> + <cuts> scaffolding is a
        # token sink; on long essays it can consume enough of the
        # max_tokens budget that the continuation body trails off
        # mid-sentence. Without this loop the partial body gets
        # accepted as-is and a judge reads a half-essay that ends in
        # the middle of a clause.
        text = await self._continue_editor_until_complete(
            db=db,
            trace=trace,
            call_id=call_id,
            round_idx=round_idx,
            initial_user_message=user_message,
            initial_response=text,
            model=model,
            editor_kwargs=editor_kwargs,
        )
        revised = _extract_continuation(text)
        # Truncated edit (closing tag still missing after continuation
        # loop, or model emitted no tags at all) → fallback. Refuse to
        # overwrite the prior draft with empty / malformed input.
        if not revised:
            revised = current_draft
        await trace.record(
            EditEvent(
                round=round_idx,
                revised_text=revised,
                revised_chars=len(revised),
                model=model,
            )
        )
        return revised

    async def _continue_editor_until_complete(
        self,
        *,
        db: DB,
        trace: CallTrace,
        call_id: str,
        round_idx: int,
        initial_user_message: str,
        initial_response: str,
        model: str,
        editor_kwargs: dict,
        max_attempts: int = 2,
    ) -> str:
        """Re-fire the editor turn-by-turn until ``<continuation>`` closes.

        Two failure modes both trigger recovery here:

        - **Truncated**: response opens ``<continuation>`` but no closer.
          The body started but max_tokens cut it off. The follow-up
          nudge says "continue from where you stopped" and we
          concatenate the new response onto the partial.
        - **Empty**: no ``<continuation>`` tag at all and the visible
          response is short or absent (the d&e audit caught this — an
          editor turn billing $0.51 with 32k output tokens but no
          visible text because adaptive thinking ate the entire
          response budget). The follow-up nudge says "you returned no
          visible output; emit the full blocks now" and we *replace*
          the empty initial response with the new one.

        Bounded by ``max_attempts`` so a pathologically verbose model
        can't loop indefinitely. Returns the (possibly concatenated)
        assistant text; caller still passes the result through
        ``_extract_continuation`` which tolerates an open trailing tag.
        """
        full = initial_response
        for attempt in range(max_attempts):
            kind = _classify_editor_response(full)
            if kind == "complete":
                return full
            if kind == "truncated":
                nudge = (
                    "Your previous response was cut off mid-continuation — "
                    "the closing </continuation> tag is missing. Continue "
                    "from exactly where you stopped (mid-sentence is fine; "
                    "do not restate or summarize the part you already wrote). "
                    "Finish the remaining sections and end with the closing "
                    "</continuation> tag."
                )
            else:  # "empty"
                nudge = (
                    "Your previous response had no visible output — the "
                    "model spent its token budget without emitting the "
                    "<preserved> / <cuts> / <continuation> blocks. "
                    "Produce the full revision now: the <preserved> note, "
                    "the <cuts> block, and the complete revised "
                    "<continuation>...</continuation> body. Keep thinking "
                    "minimal so the visible output fits within budget."
                )
            messages: list[dict] = [
                {"role": "user", "content": initial_user_message},
                {"role": "assistant", "content": full},
                {"role": "user", "content": nudge},
            ]
            more = await text_call(
                self.editor_prompt,
                messages=messages,
                metadata=LLMExchangeMetadata(
                    call_id=call_id,
                    phase=f"edit_r{round_idx}_continue{attempt + 1}",
                    round_num=round_idx,
                ),
                db=db,
                model=model,
                **editor_kwargs,
            )
            # Truncated case: append. Empty case: replace (the prior
            # response had no usable content to concatenate to).
            full = full + more if kind == "truncated" else more
        return full

    async def _scout_pass(
        self,
        *,
        db: DB,
        trace: CallTrace,
        call_id: str,
        prefix: str,
        target_length: int | None,
        model_config: ModelConfig | None,
    ) -> str:
        """Run a fast scout pass before the planner (with_scout_pass=True).

        Surfaces candidate paradigm cases + hypotheses that the planner
        can commit to as mandatory anchors. Tests the v5_hybrid
        hypothesis: anchor-density is the bottleneck for v4b on
        philosophical / less-anchor-rich essays where it loses to tp's
        research-flow architecture. The scout findings are injected
        verbatim into the planner's user message via the
        ``scout_findings`` arg on :meth:`_plan`.

        The fork validation in `.scratch/forks/v5hybrid_lockin_planner.json`
        showed planner adoption rate near 100% when scouts are
        well-structured, with the brief reorganizing the spine to land
        the new anchors rather than gluing them on top.
        """
        model = self._resolve_model(self.scout_pass_model)
        target_clause = (
            f"Target essay length: approximately {target_length} characters."
            if target_length
            else "No explicit target length provided."
        )
        user_message = (
            "<essay-prefix>\n"
            f"{prefix}\n"
            "</essay-prefix>\n\n"
            f"{target_clause}\n\n"
            "Emit the scout findings block as specified in the system "
            "prompt. The downstream planner will treat your output as "
            "candidate anchors and commit to 1-3 of them in its brief."
        )
        await trace.record(ScoutPassStartedEvent(model=model))
        text = await text_call(
            self.scout_pass_prompt,
            user_message,
            metadata=LLMExchangeMetadata(
                call_id=call_id,
                phase="scout_pass",
                round_num=None,
            ),
            db=db,
            model=model,
            cache=True,
            model_config=model_config,
        )
        findings = text.strip()
        await trace.record(
            ScoutPassEvent(
                findings_text=findings,
                findings_chars=len(findings),
                model=model,
            )
        )
        return findings

    async def _plan(
        self,
        *,
        db: DB,
        trace: CallTrace,
        call_id: str,
        prefix: str,
        target_length: int | None,
        model_config: ModelConfig | None,
        scout_findings: str | None = None,
    ) -> str:
        """Run the planner stage once before round 0.

        Emits a structural brief — spine, total target, voice
        directives, mandatory paradigm-case anchors — that downstream
        stages consume verbatim via the ``brief`` parameter on each
        stage method. The brief is text, not parsed JSON: downstream
        consumers don't depend on its internal shape, only on it
        being passed through. A planner-format regression therefore
        degrades gracefully (drafter sees odd brief text but doesn't
        crash) instead of breaking the whole workflow.
        """
        model = self._resolve_model(self.planner_model)
        target_clause = (
            f"Total target: approximately {target_length} characters."
            if target_length
            else "No explicit target length provided — pick one based on the prefix's register."
        )
        scout_block = (
            f"\n{scout_findings}\n\n"
            "The above scout findings are CANDIDATE anchors. Apply the "
            "anchor rules from your system prompt to decide how many "
            "to use, whether to augment with anchors you know "
            "independently, and how they land in the spine.\n"
            if scout_findings
            else ""
        )
        user_message = (
            "<essay-prefix>\n"
            f"{prefix}\n"
            "</essay-prefix>\n\n"
            f"{target_clause}\n"
            f"{scout_block}\n"
            "Emit the brief as specified in the system prompt. The "
            "drafter, critics, and editor will consume your output "
            "verbatim — they do not see your reasoning, only the "
            "brief itself."
        )
        await trace.record(PlannerStartedEvent(model=model))
        text = await text_call(
            self.planner_prompt,
            user_message,
            metadata=LLMExchangeMetadata(
                call_id=call_id,
                phase="planner",
                round_num=None,
            ),
            db=db,
            model=model,
            cache=True,
            model_config=model_config,
        )
        brief = text.strip()
        await trace.record(
            PlannerEvent(
                brief_text=brief,
                brief_chars=len(brief),
                model=model,
            )
        )
        return brief

    async def _arbitrate(
        self,
        *,
        db: DB,
        trace: CallTrace,
        call_id: str,
        round_idx: int,
        prefix: str,
        current_draft: str,
        critiques: Sequence[str],
        prior_arbitrations: Sequence[str],
        target_length: int | None,
        brief: str | None,
        model_config: ModelConfig | None,
        audit_brief: str | None = None,
    ) -> str:
        """Run the arbiter stage between critique and edit per round.

        Triages the round's critic notes into accept / reject /
        unresolved. The editor consumes the arbitration text in place
        of raw critiques, which closes the round-N relitigation loop
        observed in the iterate session: e.g. critics flagging the
        same redundant section across 3 rounds, editor defending it
        twice before finally cutting in round 3. With arbitration the
        cut lands in one round.

        Threads ``prior_arbitrations`` so REJECTED items stay
        rejected across rounds without the editor rediscovering them.
        """
        model = self._resolve_model(self.arbiter_model)
        critiques_block = "\n\n---\n\n".join(
            f"## Critic {i + 1}\n\n{c}" for i, c in enumerate(critiques)
        )
        current_chars = len(current_draft)
        if target_length:
            length_status = (
                f"Current draft: {current_chars} characters. "
                f"Target: {target_length} characters. "
                f"Delta: {current_chars - target_length:+d}."
            )
        else:
            length_status = f"Current draft: {current_chars} characters. (No explicit target.)"
        brief_block = f"<brief-from-planner>\n{brief}\n</brief-from-planner>\n\n" if brief else ""
        if audit_brief:
            brief_block += f"<brief-audit>\n{audit_brief}\n</brief-audit>\n\n"
        if prior_arbitrations:
            prior_block = (
                "<prior-arbitrations>\n"
                + "\n\n---\n\n".join(prior_arbitrations)
                + "\n</prior-arbitrations>\n\n"
            )
        else:
            prior_block = ""
        user_message = (
            "<essay-prefix>\n"
            f"{prefix}\n"
            "</essay-prefix>\n\n"
            f"{brief_block}"
            f"{prior_block}"
            "<current-draft>\n"
            f"{current_draft}\n"
            "</current-draft>\n\n"
            "<critiques>\n"
            f"{critiques_block}\n"
            "</critiques>\n\n"
            f"## Length\n\n{length_status}\n\n"
            f"Triage these critic notes for round {round_idx}. Emit "
            "the arbitration block as specified in the system prompt; "
            "honor any prior arbitrations shown above (REJECTED items "
            "stay rejected unless the current critic produced new "
            "evidence)."
        )
        await trace.record(
            ArbitrationStartedEvent(
                round=round_idx,
                model=model,
                n_critiques=len(critiques),
            )
        )
        text = await text_call(
            self.arbiter_prompt,
            user_message,
            metadata=LLMExchangeMetadata(
                call_id=call_id,
                phase=f"arbiter_r{round_idx}",
                round_num=round_idx,
            ),
            db=db,
            model=model,
            cache=False,
            model_config=model_config,
        )
        arbitration = text.strip()
        await trace.record(
            ArbitrationEvent(
                round=round_idx,
                arbitration_text=arbitration,
                arbitration_chars=len(arbitration),
                prior_arbitrations_seen=len(prior_arbitrations),
                model=model,
            )
        )
        return arbitration

    async def _audit_brief(
        self,
        *,
        db: DB,
        trace: CallTrace,
        call_id: str,
        after_round: int,
        prefix: str,
        original_brief: str,
        current_draft: str,
        target_length: int | None,
        model_config: ModelConfig | None,
    ) -> str:
        """Run the brief-audit stage once after a designated round.

        Emits a descriptive audit brief (same `<brief>` schema as the
        planner) populated from observation of the current draft —
        what spine the draft actually has, what anchors actually
        landed, where the voice held vs drifted. Downstream critic /
        arbiter / editor see both the original and audit briefs
        side-by-side; the drift between them surfaces structural
        decisions the editor was anchored away from.

        Threads ``after_round`` so the trace event records when the
        audit fired in the workflow timeline.
        """
        model = self._resolve_model(self.audit_model)
        target_clause = (
            f"Total target: approximately {target_length} characters."
            if target_length
            else "No explicit target length."
        )
        user_message = (
            "<essay-prefix>\n"
            f"{prefix}\n"
            "</essay-prefix>\n\n"
            f"<original-brief>\n{original_brief}\n</original-brief>\n\n"
            "<current-draft>\n"
            f"{current_draft}\n"
            "</current-draft>\n\n"
            f"{target_clause}\n\n"
            f"This is the draft after round {after_round}'s edit. Emit "
            "the audit brief as specified in the system prompt — "
            "describe what the draft has actually become. The "
            "downstream stages will see your audit alongside the "
            "original brief and decide what to act on."
        )
        await trace.record(BriefAuditStartedEvent(after_round=after_round, model=model))
        text = await text_call(
            self.audit_prompt,
            user_message,
            metadata=LLMExchangeMetadata(
                call_id=call_id,
                phase="brief_audit",
                round_num=None,
            ),
            db=db,
            model=model,
            cache=False,
            model_config=model_config,
        )
        audit = text.strip()
        await trace.record(
            BriefAuditEvent(
                after_round=after_round,
                audit_brief_text=audit,
                audit_brief_chars=len(audit),
                model=model,
            )
        )
        return audit

    def _resolve_model(self, override: str | None) -> str:
        """Resolve a per-role model override.

        Precedence: explicit constructor kwarg → ``rumil_model_override``
        (the standard ``run_versus`` path sets this via
        :func:`override_settings`) → fail-loud. Per ``versus/AGENT.md``:
        "Model for orch is passed explicitly through the bridge ... do
        not rely on ``settings.model``." Silently falling back to ambient
        ``settings.model`` would let non-bridge instantiations (tests,
        future scripts) use whatever happened to be in settings; better
        to fail fast.
        """
        if override is not None:
            return override
        rmo = get_settings().rumil_model_override
        if rmo:
            return rmo
        raise RuntimeError(
            "DraftAndEditWorkflow requires a model — pass via constructor "
            "(drafter_model / critic_model / editor_model) or via "
            "override_settings(rumil_model_override=...) (the run_versus "
            "path sets this automatically from its `model` arg)."
        )
