# Judgement Supersession

## Context

Judgement nodes represent synthesized positions — the "bottom line" of a branch. When a branch evolves (new evidence, releveled claims), the old judgement may no longer reflect the picture. New judgements supersede old ones, creating a chain.

## What exists

- `judgement` node type in schema
- `superseded_by` column on nodes table (nullable FK to nodes)
- Frontend type has `superseded_by?: string | null`
- No code yet that handles supersession

## What to build

### 1. Supersession on creation

When the orchestrator creates a judgement node under a parent, check if there's an existing active (non-superseded) judgement under the same parent. If so, set `superseded_by` on the old one to point to the new one.

In `orchestrator/tools.py` `_exec_add_node`:
```python
if inp.get("node_type") == "judgement" and not dry:
    # Find and supersede any existing active judgement under the same parent
    old = conn.execute(
        "SELECT id FROM nodes WHERE parent_id = ? AND node_type = 'judgement' AND superseded_by IS NULL AND id != ?",
        (parent_full, node_id),
    ).fetchone()
    if old:
        conn.execute("UPDATE nodes SET superseded_by = ? WHERE id = ?", (node_id, dict(old)["id"]))
```

### 2. Hide superseded nodes in tree views

Superseded nodes should not appear in the normal tree view. Update `get_subtree` in `orchestrator/context.py` to filter them out (or add a parameter to include/exclude).

In the API tree endpoint, filter superseded nodes:
```python
children = conn.execute(
    "SELECT * FROM nodes WHERE parent_id = ? AND superseded_by IS NULL ORDER BY position",
    (node_id,),
).fetchall()
```

### 3. Judgement history view

An optional UI to see the chain of judgements on a branch:
- Show current judgement prominently
- "Previous judgements" expandable section showing the chain
- Each shows when it was created and what superseded it

### 4. Visual treatment

Judgement nodes in the tree should look different from claims:
- Maybe a subtle border or background tint using `--node-judgement` / `--node-judgement-bg`
- A "judgement" badge more prominent than regular type labels
- Show "supersedes {old_id}" if it replaced something

### Files to modify
- `public-ui/orchestrator/tools.py` — supersession logic in _exec_add_node
- `public-ui/orchestrator/context.py` — filter superseded from get_subtree (optional)
- `public-ui/serve.py` — filter superseded in tree endpoint
- `public-ui/src/components/WorldviewNode.tsx` — judgement rendering
- `public-ui/src/app/globals.css` — judgement styles
