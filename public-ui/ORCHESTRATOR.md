# Real Orchestrator — Implementation Spec

The orchestrator infrastructure exists in serve.py but the chat tool
(`run_orchestrator`) returns mock health diagnostics instead of actually
calling the LLM. This doc covers wiring up the real orchestrator and
adding a multi-step loop.

## What exists

In `serve.py`:
- `run_orchestrator_step()` — full implementation that calls the Anthropic
  API with `ORCHESTRATOR_TOOLS` (add_node, suggest_change, relevel_node,
  inspect_branch). Currently only called from `POST /api/workspaces/{name}/orchestrate`.
- `execute_orchestrator_tool()` — handles all four tools, supports dry_run.
- `pick_next_branch()` — simple prioritizer picking the branch with worst
  health score.
- `get_branch_context()` — builds scoped context (root + ancestors +
  filtered subtree + sibling headlines).
- `get_branch_health()` — diagnostic counts (nodes by type, depth, gaps).

## What to change

### 1. Wire `run_orchestrator` chat tool to the real implementation

In `execute_tool()`, the `run_orchestrator` case currently returns mock
data. Replace it with an actual call to `run_orchestrator_step()`:

```python
elif name == "run_orchestrator":
    target_short = tool_input.get("node_id")
    dry = tool_input.get("dry_run", True)
    target_full = resolve_node_id(conn, target_short) if target_short else pick_next_branch(conn, ws_id)
    if not target_full:
        output = "No branch to orchestrate."
    else:
        # Actually run the orchestrator
        result = await run_orchestrator_step(
            conn, ws_id, target_full, run_id, api_key, dry_run=dry
        )
        actions_summary = "\n".join(
            f"  {a['tool']}: {a['result'][:100]}"
            for a in result["actions_taken"]
        )
        output = (
            f"Orchestrator completed ({len(result['actions_taken'])} actions):\n"
            f"{actions_summary}\n\n"
            f"{result['response'][:500]}"
        )
```

Note: `execute_tool` is sync but `run_orchestrator_step` is async.
The chat endpoint (`async def chat()`) calls `execute_tool` — make
`execute_tool` async or extract the orchestrator case to the chat handler.

### 2. Multi-step orchestrator loop

Add a `POST /api/workspaces/{name}/orchestrate-loop` endpoint that runs
the orchestrator multiple times, picking a new branch each step:

```python
@app.post("/api/workspaces/{name}/orchestrate-loop")
async def orchestrate_loop(
    name: str,
    steps: int = 3,
    dry_run: bool = True,
):
    results = []
    for i in range(steps):
        # Pick next branch (may change as tree evolves)
        branch_id = pick_next_branch(conn, ws_id)
        if not branch_id:
            break
        result = await run_orchestrator_step(
            conn, ws_id, branch_id, run_id, api_key, dry_run=dry_run
        )
        results.append({
            "step": i + 1,
            "branch": branch_headline,
            **result,
        })
    return {"steps_completed": len(results), "results": results}
```

### 3. Smarter branch prioritization

The current `pick_next_branch()` uses a simple heuristic:
```
score = total + evidence*2 - no_credence*3 - leafs_without_content*2
```

Improve this to consider:
- **Staleness**: branches not investigated recently should score higher
- **Importance**: L0 branches matter more than L3 branches
- **Pending suggestions**: branches with unresolved suggestions need attention
- **User interest**: branches the user has been chatting about recently

```python
def pick_next_branch(conn, ws_id):
    # Score each L0 branch
    for child in l0_children:
        health = get_branch_health(conn, child.id)
        last_run = get_last_run_time(conn, child.id)
        staleness = (now - last_run).hours if last_run else 999
        pending = count_pending_suggestions(conn, child.id)

        score = (
            health["no_credence"] * 3
            + health["leafs_without_content"] * 2
            + (1 if health["evidence"] < health["claims"] else 0) * 2
            + min(staleness / 24, 5)  # up to 5 points for staleness
            + pending * 2
        )
```

### 4. Orchestrator prompt improvements

The current orchestrator system prompt is functional but basic. Consider:

- **Reference the chat prompt style** — "knowledgeable colleague" tone
- **Be more specific about what L-levels mean** — when to assign L0 vs L3
- **Add examples** of good vs bad additions
- **Tension detection** — explicitly ask the model to look for contradictions
  between sibling branches, not just gaps within a branch
- **Budget awareness** — the orchestrator should know roughly how many
  actions are appropriate per step (3-5, not 15)

### 5. Safety and cost controls

- **Budget cap**: max actions per step (currently limited by tool loop rounds=8)
- **Cost tracking**: log the orchestrator's own API cost in the run record
- **Rate limiting**: don't let the chat tool fire orchestrator steps faster
  than 1 per minute
- **Confirmation for non-dry-run**: the chat model should explain what the
  orchestrator will do and confirm before running with `dry_run=false`

## Testing

1. Run orchestrator in dry_run on the default workspace — verify it
   produces sensible plans
2. Run with dry_run=false — verify nodes actually appear in the tree
3. Run the loop (3 steps) — verify it picks different branches
4. Chat: "run the orchestrator on governance" — verify end-to-end
5. Check that suggestions appear in the review queue
6. Accept a suggestion — verify it applies correctly
