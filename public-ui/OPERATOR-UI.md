# Operator UI — Implementation Spec

A separate Next.js app for developers and operators. Superset of the
public UI — same worldview rendering and chat, plus trace inspection,
quality tools, and raw graph access.

## Architecture

```
operator-ui/            separate Next.js app
  src/
    app/                same app router structure
    components/         imports shared components + operator-only ones
    lib/                shared types + operator API calls

public-ui/
  src/components/       shared components (WorldviewNode, CredenceBadge, etc.)
```

When building operator-ui, extract shared components into a local package
or use TypeScript path aliases pointing into public-ui/src/components.
The public-ui bundle must never contain operator code.

The operator-ui talks to the same serve.py backend. Operator-only
endpoints are gated by an auth header (see Auth section).

## What operator-ui adds over public-ui

### 1. Trace Inspector

View the history of what the orchestrator and chat did.

**Data source**: `GET /api/runs` (already exists) + `GET /api/runs/{id}/actions`

**UI**:
- List of runs with type (chat/orchestrate), timestamp, status, action count
- Click a run → expand to show all actions
- Each action shows: tool name, input, output, timestamp
- Filter by run type, workspace

**Component**: `TraceInspector.tsx`

```
┌─────────────────────────────────────────────┐
│  Runs                                        │
│  ┌─────────────────────────────────────────┐ │
│  │ chat · 12:44 PM · 3 actions · completed│ │
│  │  search_workspace("governance")         │ │
│  │  get_node("7ac72919")                   │ │
│  │  create_node(uncertainty, "What would…")│ │
│  └─────────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────┐ │
│  │ orchestrate · 12:30 PM · 7 actions      │ │
│  │  inspect_branch()                       │ │
│  │  add_node(evidence, "Policy analysis…") │ │
│  │  ...                                    │ │
│  └─────────────────────────────────────────┘ │
└─────────────────────────────────────────────┘
```

### 2. Quality Dashboard

Aggregate health metrics across all branches.

**Data source**: new endpoint `GET /api/workspaces/{name}/health` that runs
`get_branch_health()` on every L0 branch and returns a summary.

**UI**:
- Per-branch health cards: node counts by type, depth, gaps
- Highlight branches with issues (no credence, thin evidence)
- Overall stats: total nodes, pending suggestions, recent runs

**Component**: `QualityDashboard.tsx`

### 3. Node Editor

Direct editing of node properties — headline, content, credence,
robustness, importance, node_type. For operators who want to fix things
without going through the chat.

**Data source**: new endpoint `PATCH /api/nodes/{id}` that updates fields.

**UI**:
- Click any node in the worldview → editor panel slides in
- Form fields for each editable property
- Save button commits the change
- Show edit history (from runs/actions log)

**Component**: `NodeEditor.tsx`

### 4. Workspace Management

- Delete workspaces
- Export workspace as JSON
- Import workspace from JSON
- Duplicate workspace (for experimentation)

**Endpoints needed**:
```
DELETE /api/workspaces/{name}
GET    /api/workspaces/{name}/export    → JSON dump
POST   /api/workspaces/import          ← JSON upload
POST   /api/workspaces/{name}/duplicate?new_name=...
```

### 5. Cost Tracking

Show API costs per run, per workspace, cumulative.

**Data source**: the Anthropic API returns token usage. Store in the runs
table (add `input_tokens`, `output_tokens`, `cost_usd` columns).

**UI**: cost column in trace inspector, total in quality dashboard header.

### 6. Power Chat

Same chat panel as public-ui but with additional tools:
- `view_trace(run_id)` — show what a run did
- `edit_node(node_id, field, value)` — direct edits
- `delete_node(node_id)` — remove a node
- `reset_workspace()` — clear all nodes, re-seed
- `export_workspace()` — dump to JSON

These are destructive and should require confirmation.

## Auth

The operator-ui needs authentication. serve.py should check for an
`Authorization` header on operator-only endpoints.

Simple approach for now:
```python
OPERATOR_TOKEN = os.environ.get("OPERATOR_TOKEN", "")

def require_operator(request: Request):
    if not OPERATOR_TOKEN:
        return  # no auth configured, allow all
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if token != OPERATOR_TOKEN:
        raise HTTPException(401, "Unauthorized")
```

Public endpoints (workspaces list, tree, chat) don't need auth.
Operator endpoints (delete, edit, export, cost data) do.

## Shared components to extract

When building operator-ui, these components from public-ui should be
shared (either via a package or symlinks):

- `WorldviewNode.tsx` — node card rendering
- `CredenceBadge.tsx` — credence/robustness display
- `NodeTypeLabel.tsx` — type label + color
- `StackedPanes.tsx` — pane navigation
- `ArticleView.tsx` — article view
- `VerticalView.tsx` — vertical view
- `ChatPanel.tsx` — chat panel (operator version extends with more tools)
- `SlashCommands.tsx` — slash command dropdown
- `SuggestionReview.tsx` — review queue

The types in `lib/types.ts` and API functions in `lib/api.ts` are also
shared.

## Implementation order

1. **Scaffold operator-ui** — new Next.js app, copy config from public-ui
2. **Trace inspector** — most immediately useful, data already exists
3. **Quality dashboard** — one new endpoint + simple UI
4. **Node editor** — one new endpoint + form UI
5. **Power chat** — extend chat tools
6. **Auth** — add before any deployment
7. **Cost tracking** — needs schema change + API integration
8. **Workspace management** — convenience features, lower priority

## Port convention

Following the existing pattern:
- public-ui: port 3100
- operator-ui: port 3101
- serve.py: port 8099
