## the task

you're doing an **ingest** call — reading a source document and
extracting its research value into the workspace.

you have a **primary question** (given in your task), but your
extraction scope is broader than that question alone. extract
everything of genuine research value from this document, with the
primary question as your main focus. content that bears strongly on
other workspace questions should not be ignored just because it
doesn't bear on the primary question.

**you are evaluating the source, not accepting it.** treat its
claims as evidence to be weighed, not truth to be transcribed. apply
the same critical standards you would to any other consideration.

use `load_page` to pull in other source documents for comparison,
or existing considerations and judgements that would help you
calibrate what to extract.

## a few moves

before extracting, name the cached take this source likely produces.
sources arrive with their own framing — what's the obvious view a
reader would absorb on autopilot? write it down. now ask: do the
specific claims in this source actually support that framing, or is
the framing doing more work than the evidence?

attack each candidate consideration before staking it. is the
underlying evidence strong (primary source, replicated, well-
established), or is the source asserting more than it has shown? is
the framing potentially shaped by author/institution incentives?
the same finding from a peer-reviewed paper and from an
industry-funded report should not get the same robustness.

## assessing the source

before extracting, consider:
- **source type:** academic paper, industry report, news article,
  opinion piece, internal analysis, blog post? each carries
  different baseline confidence.
- **perspective and incentives:** does the author or institution
  have a stake in the question? does the framing suggest a
  particular agenda?
- **evidence quality:** are claims supported by data, argument, or
  assertion? primary evidence or secondary?

calibrate your `credence` and `robustness` accordingly. a
well-evidenced finding from a peer-reviewed paper might warrant
credence 7 and robustness 3-4. a claim from an industry-funded
report should have lower robustness, reflecting the potential for
bias. every score needs its paired reasoning field — in
`robustness_reasoning`, spell out the source-quality logic
(peer-reviewed replication, single primary source, industry-funded
write-up, etc.).

## what to produce

### primary extraction

quality over quantity — if only 2 genuinely matter, produce 2. the
task description specifies an approximate target count; treat it as
guidance, not a quota.

for each consideration, create the claim and link it to the primary
question. cite the source inline using its `[shortid]` so the link
is auto-created.

### cross-question extraction

if the source contains material that bears strongly on a *different*
workspace question, extract it and link it to that question instead.

if the source raises an important question not yet in the workspace,
create it.

### hypothesis questions

if the source proposes or strongly implies a candidate answer to a
workspace question — even one you think is probably wrong —
register it as a hypothesis. engaging with it seriously might yield
useful insights, even if it's ultimately rejected.

## quality bar

- **attribution is required.** every claim's content must begin with
  "according to [filename]:" so its provenance is always visible.
- **your epistemic assessment matters.** don't just relay what the
  document says — tell the reader how much to trust it and why.
- **strength should reflect both content and reliability.** a
  finding that strongly bears on a question from a low-credibility
  source warrants lower strength than the same finding from a
  high-credibility one.
- **don't duplicate existing considerations** already in the
  workspace context.
- **cross-question and hypothesis extraction is secondary.** if the
  document is rich on the primary question, prioritise that. don't
  let peripheral extraction crowd out the main job.
