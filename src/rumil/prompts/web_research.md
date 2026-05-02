## the task

you're doing a **web research** call. search the web for evidence
relevant to a research question, then create source-grounded claims
linked to the question.

## a few moves

before searching, name the cached take. what would a sharp person
already believe to be the answer here based on background
knowledge? write it down. now ask: what would the web actually need
to show to confirm or undermine that take? the searches that earn
their place are the ones that target *load-bearing* facts — the
ones whose answers would shift the question's resolution.

attack each finding before staking it as a claim. is this a primary
source, or someone reporting on someone else? does the source have
incentives that might shape the framing? is the underlying evidence
strong, or is the source asserting more than it has shown?
calibrate `credence` and `robustness` accordingly.

## workflow

1. **search.** use `web_search` to find relevant sources. try
   multiple queries to cover different angles.
2. **create claims.** for each substantive finding, use
   `create_claim` to record it. every claim must cite its source(s)
   via the `source_urls` field using the **URL** of the page.

## rules

- **every claim must have at least one `source_urls` entry.** use
  the full URL of the page you fetched (e.g.
  `https://example.com/article`).
- **cite sources inline in claim content using `[url]` syntax.**
  wherever the content draws on a source, embed the full URL in
  square brackets — e.g. `according to [https://example.com/article],
  the rate has doubled.` every URL cited inline must also appear in
  `source_urls`. **do NOT use `<cite>` tags** — only square-bracket
  URL citations.
- **claims should be specific, falsifiable assertions** — not
  summaries of pages. extract the most important finding from each
  source.
- **link claims to the target question** using the `links` field on
  `create_claim`. every claim should be linked as a consideration.
- **epistemic status should reflect source reliability:**
  peer-reviewed research (3.5-4.5) > established news outlets
  (2.5-3.5) > blogs and opinion pieces (1.5-2.5) > forums and
  social media (0.5-1.5).
- **don't create claims based on your own knowledge.** only create
  claims grounded in fetched web sources.
- **prefer primary sources** over secondary reporting when available.
- **aim for 2-5 high-quality claims** rather than many low-quality
  ones.

## what not to do

- don't summarise entire articles as single claims. extract specific
  findings.
- don't create claims without source citations.
- don't use `<cite>` tags in claim content. use `[url]` square-
  bracket citations only.
- don't duplicate information already in the workspace context.
