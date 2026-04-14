# Importance systems divergence (post-merge with main, 2026-04-13)

Two parallel implementations of "what's important in this research" now coexist in the codebase. They were developed independently and cover overlapping territory. This doc captures what each one does, why they diverge, and the open decision about which should win.

## System A: `page.importance` (brian/ui lineage)

**Scale:** 0–4, called L0–L4.
- L0 = core worldview finding
- L1 = important supporting detail
- L2 = relevant detail (default)
- L3 = supplementary
- L4 = deep supplementary

**Where it lives:** `pages.importance` column. A global property of every page.

**Who reads it:**
- `src/rumil/views.py` — builds a computed "view" of a question by filtering pages by `page.importance` and `importance_threshold`. Produces sections like `core_findings`, `supporting_detail`, `promotion_candidates`, `demotion_candidates`.
- `src/rumil/worldview.py` — presumably similar.
- `src/rumil/api/app.py` `/api/questions/{id}/view` endpoint surfaces this to the parma frontend.

**Who writes it:**
- `src/rumil/moves/update_epistemic.py` — `importance` field on payload, calls `db.update_page_importance`.
- `src/rumil/api/app.py:648-653` — RELEVEL suggestion apply path, calls `db.update_page_importance`.
- `parma/orchestrator/tools.py:212` — parma's `relevel_node` tool produces RELEVEL suggestions or direct relevels.

**Semantics:** editorial "how central is this page to the big picture" — independent of credence and robustness. Lets parma's orchestrator reshape a question's importance landscape over time.

## System B: `link.importance` on VIEW_ITEM links (main lineage)

**Scale:** 1–5 (no named levels).
- 5 = Essential (shown in NL summary)
- 4 = Important context
- 3 = Useful background
- 2 = Noted but not load-bearing
- 1 = Marginal

**Where it lives:** `page_links.importance` column, meaningful only on links with `link_type = VIEW_ITEM`. A property of *this item's place in this View*, not the item page itself.

**Who reads it:**
- `src/rumil/context.py`, `src/rumil/calls/context_builders.py`, `src/rumil/calls/closing_reviewers.py` — format View Items with `I{n}` markers, filter by `min_importance`.
- `src/rumil/database.py` — read/write VIEW_ITEM links with importance.

**Who writes it:**
- `src/rumil/moves/create_view_item.py` — creates a scored item + link (importance required).
- `src/rumil/moves/propose_view_item.py` — creates an unscored proposal (importance=None), with a docstring comment that "the next assess call will triage it." No explicit update path is visible in the current code.

**Semantics:** "how central is this atomic claim within *this* View?" — scoped to a single materialized View page, not global. Views have per-importance-level caps (`view_importance_5_cap`, etc. in settings).

## Why they diverge

System A is part of brian/ui's parma integration: parma's orchestrator operates on a question by reshaping page-level importance and computing views dynamically. Views are ephemeral — recomputed per request by `views.py`.

System B is main's `view-pages` feature: Views are first-class pages produced by a dedicated `create_view` call type. The View page persists, and its items (also pages) are organized via typed links that carry section, position, and importance metadata.

The two answer the same question ("what matters about this research?") at different layers of the stack.

## Current state (post-merge, narrow 1a applied)

- The preamble now describes **only System B** (View Items, 1–5). Rumil research LLMs no longer see L0–L4 framing.
- `update_epistemic` had its `importance` field stripped (narrow 1a): rumil research LLMs can no longer emit page-level importance updates.
- System A's operational code (`db.update_page_importance`, RELEVEL suggestions, parma `relevel_node`, `views.py`, the computed-view endpoint) is **still functional**. Parma continues to work unchanged.
- `page.importance` column and existing data are untouched.

So the two systems now coexist cleanly: rumil research calls produce/consume System B; parma (separate process) produces/consumes System A.

## Open decision

Which system should win long-term?

**Option 1: System B wins (main's View pages).** Tear out System A:
- Remove `views.py`, `worldview.py`, the computed-view endpoint.
- Remove parma's `relevel_node` tool and RELEVEL suggestion type.
- Eventually drop `pages.importance` column (migration).
- Rewire parma to build on top of main's materialized Views — either by dispatching `create_view` calls or by writing its own view pages.

Pros: one concept of importance, consistent with the rest of the Views architecture.
Cons: parma loses its current reshaping capability; needs a rewrite to use Views instead of page importance.

**Option 2: System A wins (parma-era page importance).** Tear out System B:
- Remove View pages, View Items, `create_view` call type, propose/create view item moves.
- Roll parma's computed views into the rumil preamble as the canonical "view" concept.

Pros: leaves parma untouched, simpler data model (no per-link importance).
Cons: throws out main's just-landed view-pages feature.

**Option 3: Keep both.** Accept that "View Item importance" and "page importance" are genuinely different concepts and both useful. Reframe the docs accordingly.

Pros: no rewrite needed.
Cons: two ways to say "important" is a lasting source of confusion for future instances and humans.

## Other remaining follow-ups from the merge

- `src/rumil/moves/create_view_item.py:30` — pyright flags `importance: int` overriding `CreatePagePayload.importance: int | None`. Fix by making the field declaration compatible (e.g. narrow via validator) or widening the override.
- `src/rumil/api/chat.py:348` — referenced `CallType.CLAUDE_CODE_DIRECT` which exists only on `brian/skills-towards-plugin`. Being addressed separately by adding `CallType.CHAT_DIRECT`.
