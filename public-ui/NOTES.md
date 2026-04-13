# Worldview UIs — Design & TODO

Two new UIs (separate from existing `frontend/`), plus API work to back them.

## Architecture

```
public-ui/         separate Next.js app + FastAPI backend (serve.py)
                   self-contained: own SQLite DB, own Anthropic API integration
                   no dependency on rumil core — intentionally separate for now
operator-ui/       separate Next.js app — superset of public features + dev tools
                   (not yet created)
packages/          shared components extracted when operator-ui needs them
                   (not yet created)
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

**Stacked Panes** ✓
- Horizontal pane navigation, depth-cycling active colors
- Continuous color from active card → detail pane
- URL state in `?panes=` param (dot-notation paths)
- Best for: deep exploration on wide screens

**Article View** ✓
- Single scrollable column, everything above supplementary level inline
- ToC sidebar nav with IntersectionObserver-tracked active state
- Supplementary material (L3+) collapsed in `<details>` elements
- Best for: reading start-to-finish, sharing, printing

**Vertical View** ✓
- Single column with indentation for depth
- Expanding a node indents its children below
- No side-by-side panes — depth shown through nesting
- URL state in `?expanded=` param
- Best for: narrow viewports, mobile, quick scanning

### Chat Panel ✓
- Right-side collapsible panel, ~38% width
- Scoped to current question/worldview
- Cmd+/ toggle
- Transcript style (editorial, not messaging app)
- Model selection (sonnet/opus/haiku) via slash commands
- Slash command autocomplete with arrow key nav
- Clickable 8-char node ID refs in assistant text → scroll/highlight in view
- Tool use shown inline (tool name, input, result)

This is the primary interaction surface. The worldview is the artifact the
chat helps you understand, interrogate, and extend. Users arrive, ask
questions, the system investigates, the worldview updates.

**The model is an autonomous research assistant, not a Q&A bot.** It can
reason about the workspace, decide what to investigate, and take action.
Example interactions:

- "Add a subquestion about whether export controls are sustainable long-term"
  → model creates the node, links it under the right parent

- "What's the weakest claim in this worldview?"
  → model inspects credence/robustness scores, identifies thin spots

- "Run the orchestrator on the interpretability branch"
  → model runs orchestrator step, adds/relevels nodes, queues suggestions

**Architecture — self-contained backend (serve.py):**

The API chat endpoint talks directly to Claude via the Anthropic SDK, with
tool definitions for workspace operations. All state lives in a local SQLite
DB. This is intentionally decoupled from rumil core — no dependency on the
rumil DB, skills layer, or provenance model. Future integration is possible
but not a near-term goal.

**Model tools:**

| Tool | What it does |
|------|-------------|
| search_workspace | Text search over node headlines + content |
| get_node | Fetch node by short ID with subtree |
| create_node | Add a new node to the tree |
| list_workspace | Print full tree structure |
| get_suggestions | View the review queue |
| run_orchestrator | Run orchestrator step on a branch |

**Chat TODO:**
- [ ] Show research progress when orchestrator is running
- [ ] Handle long-running orchestrator calls — progress indicator or SSE updates
- [ ] Richer inspect experience (modal/panel when clicking node refs)

### Provenance Inspect (partial)
- `/inspect <id>` slash command exists, `get_node` backend tool exists
- Missing: frontend modal/panel to display the result richly
- Could be: slide-over pane, modal, or inline popover
- Show: node content, credence/robustness, type, children

### Search ✓ (via chat)
- Text search over headlines + content, accessed via `/search` in chat
- Model searches on behalf of user, surfaces results in conversation
- No standalone search UI — chat is the primary interface

### Workspace Browser ✓
- Landing page listing workspaces with node count, run count, pending suggestions
- Click workspace → load its worldview tree
- Create new workspace + root question from the browser

### Suggestion Review ✓
- Review queue for orchestrator-generated suggestions
- Tabs: pending / accepted / rejected
- Each suggestion shows type, target node, reasoning, accept/reject buttons
- Accessible via `/review` slash command in chat

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

## API Status (serve.py)

### Implemented
- [x] `POST /api/chat` — tool-calling chat with Claude, up to 10 tool rounds
- [x] `POST /api/chat/stream` — SSE streaming variant
- [x] `GET /api/workspaces` — list workspaces with stats
- [x] `POST /api/workspaces` — create workspace + root question
- [x] `GET /api/workspaces/{name}/tree` — full worldview tree (nested JSON)
- [x] `GET /api/workspaces/{name}/suggestions` — review queue by status
- [x] `POST /api/suggestions/{id}/accept|reject` — act on suggestions
- [x] `GET /api/workspaces/{name}/branch-context/{node_id}` — branch context + health
- [x] `POST /api/workspaces/{name}/orchestrate` — run orchestrator step (dry_run supported)
- [x] `GET /api/runs` — run history
- [x] `GET /api/runs/{run_id}/actions` — actions within a run

### Not yet built
- [ ] SSE/WebSocket for orchestrator progress (currently fire-and-forget)
- [ ] Source ingestion endpoint (ingest a URL, extract into nodes)
- [ ] Worldview caching/storage — currently rebuilds tree on every request

### Auth
- No auth yet — everything open for local dev
- Public UI: eventually authenticated (track who initiated what)
- Operator UI: authenticated + admin role

## Shared Components (extract when building operator-ui)

- WorldviewNode renderer (card, credence badge, type label)
- Stacked panes navigation
- Article view + ToC sidebar
- Vertical view
- Chat panel + slash commands
- Suggestion review
- View mode switcher
- Workspace browser

## Open Questions

- Should the public UI eventually connect to rumil core, or stay independent?
- How to handle worldview staleness — show "generated at" timestamp, or auto-refresh?
- What's the right supplementary boundary? Fixed depth, or LLM-decided per tree?
- Source ingestion — how should URLs/documents enter the system?
- Multi-user — do different users see each other's chat history / suggestions?
