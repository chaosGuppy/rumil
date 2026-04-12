# Worldview UIs — Design & TODO

Two new UIs (separate from existing `frontend/`), plus API work to back them.

## Architecture

```
public-ui/         separate Next.js app — auditable, no operator code
operator-ui/       separate Next.js app — superset of public features + dev tools
                   (not yet created)
packages/          shared components extracted when operator-ui needs them
                   (not yet created)

src/rumil/api/     existing read-only API — needs new endpoints for both UIs
src/rumil/worldview.py   worldview generation (exists, no API endpoint yet)
```

Security boundary: public app literally cannot render traces/prompts/orchestration
because that code doesn't exist in its bundle.

## Design Principles

See memory file `project_worldview_ui_principles.md` for the full set.
- Safe to click — never lose place
- Deep linkable — URL encodes view state
- Epistemic honesty — credence/robustness first-class
- Provenance on demand — "why does it think this?" always one click away
- Consciously experimental — new artifact type, not pretending to be human-written
- Context-dependent navigation — small detail inline, large detail in pane
- Minimal chrome — content is the interface

## Public UI

### View Modes

Three ways to render the same worldview tree. User picks, URL encodes the mode.

**Stacked Panes (current)**
- Horizontal pane navigation, depth-cycling active colors
- Continuous color from active card → detail pane
- Best for: deep exploration on wide screens

**Article View (TODO)**
- Single scrollable column, everything above supplementary level inline
- ToC sidebar nav
- Supplementary material collapsed/expandable at end of each section
- Best for: reading start-to-finish, sharing, printing

**Vertical View (TODO)**
- Single column with indentation for depth
- Expanding a node indents its children below
- No side-by-side panes — depth shown through nesting
- Best for: narrow viewports, mobile, quick scanning

### Chat Panel (in progress)
- Right-side collapsible panel, ~38% width
- Scoped to current question/worldview
- Cmd+/ toggle
- Transcript style (editorial, not messaging app)

This is the primary interaction surface. The worldview is the artifact the
chat helps you understand, interrogate, and extend. Users arrive, ask
questions, the system investigates, the worldview updates.

**The model is an autonomous research assistant, not a Q&A bot.** It can
reason about the workspace, decide what to investigate, and take action.
Example interactions:

- "Gather context on the question about compute governance and see if
  dispatching anything on the graph would help"
  → model searches workspace, reads relevant pages, inspects graph shape,
    then proposes or fires specific calls (find_considerations, scout, etc.)

- "How can we run an orchestrator to dive in more on interpretability?"
  → model finds the right question, explains what orchestrate would do,
    confirms budget, then fires it

- "Add a subquestion about whether export controls are sustainable long-term"
  → model creates the question, links it under the right parent

- "What's the weakest claim in this worldview?"
  → model inspects credence/robustness scores, traces evidence chains,
    identifies thin spots

**Architecture — reuses CC skills layer directly:**

The API chat endpoint wraps the same Python code that backs the CC skills.
Two-lane provenance model is preserved:

- **CC-mediated lane**: direct mutations via chat envelope (apply_move).
  CREATE_QUESTION, CREATE_CLAIM, LINK_*, FLAG. Cheap decisions from broad
  context. All grouped under one CLAUDE_CODE_DIRECT Call for provenance.

- **Rumil-mediated lane**: full pipeline calls via dispatch_call.py.
  find_considerations, assess, scout-*, web_research. Expensive structured
  investigation through rumil's context builders, prompts, and agent loops.

**Model tools:**

| Tool | What it does | Lane |
|------|-------------|------|
| search_workspace | Embedding search, returns relevant pages | read |
| get_page | Fetch page by short ID — content, scores, links | read |
| get_question_shape | Graph health for a question — children, considerations, gaps | read |
| create_question | Add a question to the workspace | cc-mediated |
| create_claim | Add a claim linked to a question | cc-mediated |
| link_pages | Create a link between pages | cc-mediated |
| dispatch_call | Fire one rumil call (find_considerations, assess, etc.) | rumil-mediated |
| orchestrate | Run full orchestrator with budget | rumil-mediated |
| ingest_source | Ingest a URL as source, extract considerations | rumil-mediated |

**Context loaded at chat start** (same pattern as show_question.py):
- Worldview tree (the distilled summary)
- Research subtree (build_research_tree, depth 3)
- Embedding-based workspace neighbors
- Recent calls on the question (last 8)

**Backing code** (all in .claude/lib/rumil_skills/, importable):
- _runctx.py — DB setup, run creation, chat envelope lifecycle
- apply_move.py — cc-mediated move execution with schema validation
- dispatch_call.py — rumil-mediated call dispatch
- show_question.py — context loader (subtree + neighbors + calls)
- scan.py — graph health checks

**Chat TODO:**
- [ ] Build POST /api/chat endpoint with SSE streaming
- [ ] Wrap CC skills backing code as API-callable tools
- [ ] Create chat envelope for each API session
- [ ] Wire frontend ChatPanel to streaming endpoint
- [ ] Show tool use inline (searching, dispatching, creating)
- [ ] Show research progress when calls are dispatched
- [ ] Let model reference worldview nodes (click to scroll/expand)
- [ ] Handle long-running calls (dispatch/orchestrate) — poll or SSE updates

### Provenance Inspect (TODO)
- Click any source page ID ([f8a1b2c3]) in the worldview → see original page
- Could be: slide-over pane, modal, or inline popover
- Show: page content, credence/robustness, who created it, what call
- Needs: API endpoint to fetch page by short ID (exists: GET /api/pages/short/{short_id})

### Search (TODO)
- Embedding-based semantic search, primarily accessed through chat
- Model searches on behalf of user, surfaces results in conversation
- Also possible as standalone UI (search bar → results page)
- Needs: API endpoint for embedding search (does not exist yet)

### Question Browser (TODO)
- Landing page listing workspace questions
- Click a question → generate/load its worldview
- Can also ask a new question from here (or from chat)
- Needs: worldview generation endpoint or pre-generated worldviews

## Operator UI

Same core experience as public UI (worldview + chat + model tools) but with
visibility into the system's internals and quality controls. The chat model
has the same capabilities plus cleanup/review tools.

### Additional Operator Capabilities
- **Trace Inspector** — call trees, LLM exchanges verbatim, cost/tokens/duration.
  Integrated with worldview (click a provenance link → see the call that made it).
  Backed by: GET /api/runs/{run_id}/trace-tree, GET /api/calls/{call_id}/events
- **Research Graph Explorer** — visual graph of pages and links. Filter by type,
  credence, recency. Click node → page detail. Upgrade of current subgraph-view.
- **Quality Dashboard** — confusion scan results, structured review punch lists,
  graph health (barren questions, orphans), rating distributions.
- **Cleanup Tools** — model can run grounding/feedback pipelines from chat.
  Review → clean loop accessible through conversation.

### What Operator Chat Adds Over Public Chat
- Trace visibility: "show me the call that produced this claim"
- Quality tools: "scan this question for confusion", "run a review"
- Cleanup: "re-ground these weakly-sourced claims"
- Raw page/link inspection: full graph navigation
- Cost/budget visibility: "how much has this investigation cost?"

## API Work Needed

### For Both UIs (public chat needs all of these)
- [ ] `POST /api/chat` — streaming chat endpoint. Model gets tools for everything below.
      Context: worldview + research tree + workspace search. Streaming via SSE.
- [ ] `GET /api/questions/{id}/worldview` — generate or serve cached worldview
- [ ] `GET /api/search?q=...` — embedding-based semantic search
- [ ] `POST /api/questions` — create a new question (user asks via chat)
- [ ] `POST /api/questions/{id}/dispatch` — fire a single call type
- [ ] `POST /api/questions/{id}/orchestrate` — run orchestrator with budget
- [ ] `POST /api/questions/{id}/ingest` — ingest a source URL
- [ ] Worldview caching/storage — avoid regenerating on every request
- [ ] SSE or WebSocket for research progress (calls running, completing)

### Operator-Only Endpoints
- [ ] `GET /api/questions/{id}/health` — graph health + scan results
- [ ] `GET /api/questions/{id}/review` — structured review output
- [ ] Trace/call endpoints already exist in current API

### Auth
- Public UI: authenticated (track who initiated what, per deployment desiderata)
- Operator UI: authenticated + admin role (sees traces, costs, can cleanup)
- Current API has optional basic auth — extend with proper user tracking

## Shared Components (extract when building operator-ui)

- WorldviewNode renderer (card, credence badge, type label)
- Stacked panes navigation
- Chat panel
- Provenance popover
- View mode switcher
- Question list/browser

## Open Questions

- Should worldviews be stored as WIKI pages in the DB, or as a separate table?
- How often to regenerate worldviews? On-demand vs. after each research run?
- Should the public UI show the research graph at all, or only the distilled worldview?
- How to handle worldview staleness — show "generated at" timestamp, or auto-refresh?
- What's the right supplementary boundary? Fixed depth, or LLM-decided per tree?
