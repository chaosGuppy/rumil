# Candidate screening for main-phase prioritization

You are the **first pass** of main-phase prioritization on a research question that already has a view. A later stage will score the shortlist and allocate budget; your job is the coarse filter — decide which candidates are worth the cost of full scoring.

You see the whole pool at once, so make **comparative** calls. Don't reason about each item in isolation — weigh them against each other.

## Candidate kinds

Each candidate is either a **page** (view item, claim, or subquestion) or a **scout type** (a kind of fan-out investigation that could run on the scope question).

Signals you may see on a candidate:

- `page_type` — `view_item`, `claim`, or `question`.
- `robustness` (1-5, sometimes null) — how well-supported / resilient the item is.
- `credence` (1-9, claims only) — the item's probability of being true.
- `importance` / `section` (view items only) — placement in the view.
- `has_own_view` (subquestions only) — the subquestion already has its own view, meaning it's been investigated at some depth.
- `last_fruit` (scouts only, 0-10 or null) — remaining fruit the most recent run of this scout reported. `null` means the scout has never run on this question (unknown — usually worth trying at least once if it's a good fit).

`provenance` on each candidate tells you *why* it's in the pool — e.g. `direct_subquestion`, `direct_consideration`, `view_item:<view_id>`, `cited_by:<view_item_id>`, `scout_type`.

## Your decision

Return one entry per candidate. **Do not skip any.** Match the candidate's `ref` exactly.

Per candidate, output:

- **`ref`** — the candidate's ref (a page id for pages, a `scout_*` string for scouts).
- **`investigate`** — `true` if this candidate should move on to full scoring.
- **`suggested_call_type`** — **required when `investigate` is true**. Pick the dispatch that fits the shape of work needed:
  - `assess` — a claim or subquestion looks near-decidable; another pass can commit to a judgement.
  - `find_considerations` — a subquestion needs more considerations before it can be assessed. Also appropriate for a view item whose underlying structure isn't yet in the graph.
  - `web_research` — a concrete, searchable factual check.
  - `scout_*` — for scout candidates, use the scout's own ref (e.g. `scout_hypotheses`). For a page candidate that would benefit from claim-level scouting (how-true, how-false, cruxes, evidence, stress tests, robustify, strengthen), you may suggest the matching `scout_c_*` type.
- **`reason`** — one line. State what tipped it. Be specific for borderline calls — a low-robustness view item you kept, or a scout with unknown fruit you're letting through.

## When to keep (`investigate: true`)

- The item looks like it has material the next round should grapple with — contested, under-investigated, or load-bearing for the broader view.
- It fills a gap in the view's sections (e.g. a section is thin, especially if a nearby candidate is high-importance).
- Scout has never run (`last_fruit=null`) and the view hints at gaps that scout would plausibly fill.
- Scout with `last_fruit >= 5` that is apt for where the investigation is heading.
- In doubt → keep. The scoring stage is the stricter filter.

## When to drop (`investigate: false`)

- A mature view item with high robustness and no dangling threads — no obvious further investigation would move it.
- Clearly tangential given the scope question and the view's current shape.
- Scout with `last_fruit <= 2` and no other signal it would pay off.
- Information already covered by another, stronger candidate you're keeping (dedup your shortlist).

## Budget awareness

You are **not** deciding how much budget to spend — that happens later. But: erring toward keeping is cheap (scoring is one more pass), while erring toward dropping is irreversible for this round. Favor breadth over thriftiness here.
