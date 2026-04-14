# View Pages Design

## Overview

View pages are structured, living summaries of the current understanding on a question. They replace the ad-hoc context construction ("turning on the hose") with a curated, scored, and sectioned representation that becomes the primary context for all work on a question.

This document captures the design decisions made so far. It covers the data model, rendering strategy, and interaction patterns. It does not cover implementation sequencing — see "Worldviews for Rumil.md" for the phased rollout plan.

## Core Data Model

### View Page

A new page type (`PAGE_TYPE.VIEW`) that serves as the anchor for a question's current understanding.

- Linked to its question (via an `ANSWERS`-style link, superseding any prior judgement)
- Stores **section definitions** as structured column data (not in `extra`). A default set of sections is provided (see below), but sections are per-View and editable by future assess calls
- The `content` field holds an **LLM-written natural language summary** covering the importance-5 items — framing their interactions, tensions, and overall epistemic posture. This is re-rendered whenever the importance-5 item set changes.
- Lightweight questions that don't warrant a full View can still use traditional judgements

### View Items

A new page type (`PAGE_TYPE.VIEW_ITEM`). Each item is an atomic claim/observation/hypothesis within a View.

- Has `credence` (1-9) and `robustness` (1-5) scores, using the existing scoring infrastructure
- The `content` field carries the item's text plus an **epistemic gloss**: 1-2 sentences written by the creating instance explaining what the C/R scores mean in this specific case (e.g., "Well-supported by converging indirect evidence, but no direct experimental confirmation")
- Items are **durable** — they persist across View versions and can be shared. When a View is superseded, its items remain in the graph; the new View creates fresh links to whichever items it retains.
- Items participate in the normal link graph: they can link to evidence pages, other claims, etc. via standard `PageLink`s

### VIEW_ITEM Link

The link between a View page and a View item carries:

- **`importance`** (1-5, or null for unscored proposals): how core this item is to the View. Null means "proposed by a non-assess instance, awaiting triage."
- **`section`**: which section of the View this item belongs to
- **`position`**: ordering within the section

Importance lives on the link (not the item) because it describes the item's role in a particular View. If an item appeared in multiple Views, it could have different importance in each.

### Meta Items

Meta-level content (investigation priorities, inclusion reasoning, proposals for changes) is stored as separate pages with a meta tag. Meta items can link to:

1. **A Content item directly** — e.g., "This item is included because..." (stays with the item regardless of which View it's in)
2. **A VIEW_ITEM link / the item-in-View relationship** — e.g., "Promoted to importance-5 because of new evidence from [page]" (specific to this View's treatment of the item)
3. **The View page itself** — e.g., "Priority: investigate subquestion Y next" (about the investigation, not any particular item)

Meta items do not use C/R/I scores — they carry a type tag (priority, annotation, proposal, etc.) rather than epistemic scoring.

## Sections

Each View page stores its own section definitions. New Views are created with a default set:

- Broader context
- Confident views
- Live hypotheses
- Key evidence
- Assessments
- Key uncertainties
- Other

Sections are editable (future assess calls may restructure them), but for Phase 1, Views are created with the defaults and editing is deferred.

## Importance Caps

To prevent grade inflation and force active prioritization, each View has caps on how many items can exist at each importance tier:

| Importance | Max items | Role |
|------------|-----------|------|
| 5 | 5 | Core to the View. Covered by the NL summary. |
| 4 | 10 | Important but not central. |
| 3 | 25 | Useful context. |
| 2 | 50 | Noted but not load-bearing. |
| 1 | Uncapped | Catch-all. |

These live in settings and are enforced via the assess call's prompt (presenting current counts against caps). Total across capped tiers: 90 items.

## Rendering and Context Loading

### Rendering Stack

Views are rendered in two layers:

1. **NL summary** (stored in View page's `content`): LLM-written prose covering the importance-5 items. Focuses on interaction effects, framing, and epistemic posture rather than repeating items verbatim. Re-rendered only when the importance-5 set changes.
2. **Programmatic item rendering** (function in `context.py`): iterates VIEW_ITEM links by section, filters by importance threshold, formats each item from its content + gloss + C/R/I tag. No LLM call needed.

### Context Thresholds

Different contexts call for different depth. A parameter controls which importance threshold is applied:

| Threshold | What's shown | Typical use |
|-----------|-------------|-------------|
| 5 | NL summary only | Tight context; other questions' Views when space is limited |
| 4+ | NL summary + importance-4 items | **Default for loading other questions' Views** |
| 3+ | NL summary + importance-4 + importance-3 items | Working context for the question's own investigation |
| 2+ | Full View | Rarely used; available for deep-dive assess calls |

### Loading Other Questions' Views

When investigating question A and question B is relevant, B's View is loaded at importance 4+ by default. The threshold can be passed as a parameter for tighter or looser inclusion.

## Supersession Semantics

- When a View is superseded (during an assess call), a **new View page** is created with **fresh links** to whichever items the new version retains
- The old View's links remain intact — you can reconstruct exactly what any prior version looked like by following its links
- Items are durable and shared across versions; they are not copied or recreated unless their content actually changes
- This gives free version diffing (compare link sets between View versions) without depending on mutation event replay

## Proposing View Changes (Non-Assess Instances)

When a scout or other non-assess instance discovers something that should update the View:

- A new move type (`PROPOSE_VIEW_ITEM`) creates a `VIEW_ITEM` page and links it to the View with `importance=null`
- These unscored proposals accumulate in the View's supplementary layer
- The next assess call triages them: scoring, promoting to the main View, or discarding

## Interaction with Existing Systems

- **Existing judgements**: Still work for lightweight questions. A View supersedes any prior judgement when created, but questions that only get a quick assess can continue using the traditional judgement flow.
- **Existing page graph**: Claims, considerations, and other "squidgy layer" pages don't go away. View items link to them (via `CITES`, `BASED_ON`, etc.) for provenance. The View summarizes; the squidgy layer provides the evidence base.
- **Assess calls**: Reworked to focus on updating the View — evaluating proposals, re-scoring items, promoting/demoting across importance tiers, and triggering NL summary re-rendering when the importance-5 set changes.

## Open Questions / Future Work

- **Adversarial review for main-page changes**: The worldviews doc proposes a proposal/counter-argument/adjudication process for changes to high-importance items. Deferred to Phase 3+, but the data model (proposals as meta items, linking to the items they concern) supports it.
- **Workspace-wide worldview page**: After per-question Views are working, extend to cross-cutting Views that span multiple questions. Same data model, different scope.
- **View creation call type**: Likely needs a dedicated `CREATE_VIEW` or `INITIAL_ASSESS` call type distinct from regular assess, with a prompt optimized for producing the initial View structure from P1 scouting output.
