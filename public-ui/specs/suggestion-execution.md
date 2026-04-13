# Suggestion Auto-Execution

## Context

When a user accepts a suggestion, the status changes to 'accepted' but the actual mutation usually doesn't happen. Only `relevel_node` suggestions apply the change. `add_to_branch`, `resolve_tension`, and `merge_duplicate` suggestions are accepted but nothing further occurs.

## What exists

- `POST /api/suggestions/{id}/accept` — sets status to 'accepted', only executes relevel_node
- `POST /api/suggestions/{id}/reject` — sets status to 'rejected'
- Suggestions have: suggestion_type, target_node_id, payload (JSON with reasoning + details)
- The `history` context layer shows accepted/rejected suggestions to the orchestrator

## What to build

### 1. Execute on accept

When accepting a suggestion, apply the mutation based on `suggestion_type`:

**`add_to_branch`**: Create the node described in the payload.
```python
if sd["suggestion_type"] == "add_to_branch":
    payload = json.loads(sd["payload"])
    # Create the node from payload fields: node_type, headline, content, credence, robustness
    node_id = new_id()
    conn.execute(
        "INSERT INTO nodes (...) VALUES (...)",
        (node_id, ..., payload.get("node_type", "claim"), payload.get("headline", ""), ...),
    )
```

**`relevel_node`**: Already implemented — update importance.

**`resolve_tension`**: Create an `opposes` link between the nodes in tension, plus optionally an uncertainty node flagging the unresolved issue.

**`merge_duplicate`**: Supersede one node in favor of the other (set `superseded_by`).

### 2. Richer suggestion payloads

The orchestrator's `suggest_change` tool should produce richer payloads so acceptance can execute them. Update the tool description to guide the model:
- For `add_to_branch`: include `node_type`, `headline`, `content`, `credence`, `robustness`, `importance` in payload
- For `resolve_tension`: include both node IDs and the nature of the tension
- For `merge_duplicate`: include which node to keep and which to supersede

### 3. Suggestion review UI improvements

The `SuggestionReview.tsx` component should show a preview of what accepting will do:
- For add_to_branch: show the proposed node (type, headline, content preview)
- For relevel: show current L-level → proposed L-level
- For merge: show which node survives

### Files to modify
- `public-ui/serve.py` — expand accept_suggestion endpoint
- `public-ui/orchestrator/tools.py` — richer suggest_change description/guidance
- `public-ui/src/components/SuggestionReview.tsx` — richer preview
