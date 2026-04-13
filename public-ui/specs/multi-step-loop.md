# Multi-Step Orchestrator Loop

## Context

The orchestrator currently runs one step at a time (one branch, one run type). We need an endpoint that runs multiple steps, picking branches and alternating run types intelligently.

## What exists

- `POST /api/workspaces/{name}/orchestrate` — single step, takes run_type + node_id
- `pick_next_branch()` in `orchestrator/prioritizer.py` — simple health-score heuristic
- `resolve_run_type()` — returns config for explore/evaluate
- `RunTracer` — traces each run
- Context layers include `worldview` (L0 band + candidates) and `history` (recent runs + suggestions)

## What to build

### 1. `POST /api/workspaces/{name}/orchestrate-loop`

```python
@app.post("/api/workspaces/{name}/orchestrate-loop")
async def orchestrate_loop(
    name: str,
    steps: int = 3,
    strategy: str = "auto",  # "auto" | "explore-only" | "evaluate-only" | "alternate"
    dry_run: bool = True,
):
```

The loop:
1. For each step, pick the next branch (may change as tree evolves)
2. Decide which run type to use based on strategy + branch state
3. Run the step (with tracing)
4. Collect results
5. Return summary of all steps

### 2. Strategy: `auto` mode

The interesting part. `auto` should look at branch state to decide:
- **Branch never explored** → explore
- **Branch explored but never evaluated** → evaluate (if enough nodes)
- **Branch evaluated but L-levels look stale** → evaluate again
- **Branch recently explored AND evaluated** → skip, pick a different branch

This uses the `history` context layer logic — check recent runs on each branch.

### 3. Smarter `pick_next_branch`

Update `prioritizer.py` to consider:
- Staleness: time since last run on this branch
- Run type history: has this branch been explored? Evaluated?
- Pending suggestions: branches with unresolved suggestions need attention
- The current heuristic (health score) as a tiebreaker

### 4. Chat integration

The chat model should be able to trigger the loop:
```
User: "run 3 steps on the tree"
Model: calls run_orchestrator_loop tool
```

Add a `run_orchestrator_loop` tool to the chat TOOLS list.

### 5. Progress

The loop should emit progress events (SSE) for each step as it completes, so the chat UI can show incremental updates.

### Files to modify
- `public-ui/serve.py` — new endpoint + chat tool
- `public-ui/orchestrator/prioritizer.py` — smarter selection
- `public-ui/orchestrator/run_types.py` — strategy logic
- `public-ui/prompts/api_chat.md` — document the loop tool

### Testing
1. `POST /api/workspaces/default/orchestrate-loop?steps=3&dry_run=true` — verify it picks different branches
2. Run with dry_run=false — verify nodes appear
3. Check that auto strategy alternates explore/evaluate appropriately
4. Chat: "run 3 steps" → model triggers loop
