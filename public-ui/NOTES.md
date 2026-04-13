# Worldview UIs — Design & TODO

Two new UIs (separate from existing `frontend/`), plus API work to back them.

## Architecture

```
public-ui/                 Next.js app + FastAPI backend (serve.py)
                           self-contained: own SQLite DB, own Anthropic API integration
                           no dependency on rumil core — intentionally separate for now
  src/app/(public)/        public routes (worldview browser, views, chat)
  src/app/(operator)/      operator routes (traces, quality tools)
  src/components/          shared components
  src/components/operator/ operator-only components (never imported by public routes)
```

Operator UI lives in the same Next.js app via route groups. Next.js code-splits
by route, so operator components never appear in the public JS bundle.
Source co-location is fine — the bundle separation is what matters.

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

## Operator UI (/traces, /quality, etc.)

Same app as public UI (route groups), with operator routes at /traces etc.
Operator components in `src/components/operator/`, never imported by public routes.

### Trace Viewer ✓ (mock data, pending backend instrumentation)
- `/traces` — run list with type/status filters, cost/token summaries
- `/traces/[runId]` — full trace detail with:
  - Span-based event hierarchy (flat events reconstructed into tree)
  - ModelEventCard: model name, config (temp/max_tokens), full input messages,
    output, token breakdown (input/cache_read/cache_write/output), cost, duration
  - ToolEventCard: function name, arguments, result, error, duration
  - TokenBar: stacked horizontal bar with cache visibility
  - MessageInspector: role-colored messages, collapsible system prompt,
    tool_use/tool_result blocks as structured JSON
- Types in `src/lib/operator-types.ts` (Inspect AI-inspired event model)
- Currently uses mock data — needs serve.py `trace_events` table + instrumentation

### Trace Backend TODO
- [ ] `trace_events` table in SQLite (event_type, span_id, parent_span_id, data JSON)
- [ ] Instrument `client.messages.create`/`.stream` calls to record ModelEvents
- [ ] Instrument tool execution to record ToolEvents with timing
- [ ] `GET /api/operator/runs` — run list with aggregated cost/tokens
- [ ] `GET /api/operator/runs/{run_id}` — full trace with events
- [ ] Token usage + cost columns on `runs` table

### Future Operator Features
- **Quality Dashboard** — per-branch health, node counts, gap identification
- **Node Editor** — direct editing of node properties without chat
- **Research Graph Explorer** — visual graph navigation
- **Power Chat** — chat with additional tools (edit_node, delete_node, etc.)
- **Cost Tracking** — per-run and cumulative API costs

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

## Shared Components

All shared components live in `src/components/` and are used by both
public and operator routes. Operator-only components are in
`src/components/operator/`.

## Open Questions

- Should the public UI eventually connect to rumil core, or stay independent?
- How to handle worldview staleness — show "generated at" timestamp, or auto-refresh?
- What's the right supplementary boundary? Fixed depth, or LLM-decided per tree?
- Source ingestion — how should URLs/documents enter the system?
- Multi-user — do different users see each other's chat history / suggestions?
