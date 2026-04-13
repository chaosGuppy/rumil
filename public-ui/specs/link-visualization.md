# Link Visualization

## Context

The data model now supports typed links between nodes (`node_links` table with supports, opposes, depends_on, related). These need to be visible in the UI — both as indicators on individual nodes and as navigable connections.

## What exists

- `node_links` table: source_id, target_id, link_type, strength, reasoning
- Frontend types: `NodeLink`, `LinkType` in `src/lib/types.ts`
- `WorldviewNode` has optional `links_out` and `links_in` fields
- The API tree endpoint (`GET /api/workspaces/{name}/tree`) does NOT yet include links

## What to build

### 1. API: include links in tree response

Update the `/api/workspaces/{name}/tree` endpoint (or add a separate links endpoint) to return links. Two options:

**Option A**: Add a `links` field to the tree response alongside the node tree:
```json
{
  "id": "...",
  "children": [...],
  "links": [
    {"id": "...", "source_id": "abc", "target_id": "def", "link_type": "depends_on", "strength": 4, "reasoning": "..."}
  ]
}
```

**Option B**: Attach `links_out`/`links_in` to each node in the tree.

Option A is probably simpler — one flat list, frontend resolves.

### 2. Link indicators on nodes

Each `WorldviewNodeCard` should show a subtle indicator when the node has incoming or outgoing links:
- Small badges: "2 supports", "1 opposes", "depends on 3"
- Clicking expands to show link details (target headline, reasoning, strength)
- Color-coded: supports=green, opposes=red/amber, depends_on=blue, related=gray
- Placed near the credence/robustness badges in the node header

### 3. Link lines in panes view (stretch goal)

In StackedPanes, if two linked nodes are both visible, draw a subtle connecting line between them. This is complex (requires cross-pane coordinate tracking) — skip for v1 unless it's easy.

### 4. Depends-on chain view (stretch goal)

A way to trace the dependency chain: "this claim depends on X, which depends on Y." Could be a hover expansion or a dedicated view.

### Files to modify
- `public-ui/serve.py` — include links in tree response
- `public-ui/src/lib/api.ts` — parse links from API response
- `public-ui/src/components/WorldviewNode.tsx` — link indicators
- `public-ui/src/app/globals.css` — link badge styles
- `public-ui/src/lib/types.ts` — already has the types

### Design notes
- Links should be discoverable but not noisy — most nodes won't have links initially
- The `depends_on` links are the most important to surface (they show reasoning chains)
- `opposes` links between nodes in different branches = visible tensions (high value)
