# Cross-Check Run Type

## Context

The orchestrator works one branch at a time. Gap #5 from the vision: no cross-branch coordination. A `cross_check` run type would compare sibling branches, find tensions, redundancies, and shared assumptions.

## What exists

- `siblings` context layer shows sibling branch headlines
- `suggest_change` tool can flag cross-branch issues
- `link_nodes` tool can create `opposes`/`depends_on` links across branches
- `worldview` context layer shows the L0 band

## What to build

### 1. Cross-check prompt (`orchestrator/prompts/cross_check.md`)

Different from explore/evaluate — the orchestrator reads multiple branches at once (shallow) rather than one branch deeply. Focus areas:

- **Tensions**: do sibling branches make contradictory claims? Flag with `opposes` links
- **Shared assumptions**: do multiple branches depend on the same upstream claim? Make `depends_on` links explicit
- **Redundancy**: are different branches saying the same thing in different words? Suggest merges
- **Coverage gaps**: given the root question, are there important angles that no branch addresses? Suggest new branches
- **L0 coherence**: does the combined L0 band across all branches tell a coherent story?

### 2. Cross-check context

Needs a new context layer or different composition:
- Root question prominently framed
- ALL L0 sibling branches with their L0+L1 nodes (not just one branch deeply)
- The full L0 band across the tree
- Existing cross-branch links and suggestions

This is wider but shallower than explore/evaluate context.

### 3. Run type config

```python
"cross_check": {
    "description": "Cross-branch: find tensions, redundancies, shared assumptions",
    "prompts": ["preamble.md", "cross_check.md"],
    "tool_set": "cross_check",  # link_nodes, suggest_change, inspect_branch only
    "context_layers": ["root", "all_branches_shallow", "worldview", "pending"],
    "runner": {"max_rounds": 6, "temperature": 0.4},
}
```

Tool set: `link_nodes` + `suggest_change` + `update_node` + `inspect_branch`. No `add_node` — cross-check observes and links, doesn't create.

### 4. `all_branches_shallow` context layer

New layer in `context.py` that renders all top-level branches at L0+L1 depth. Different from `branch` (which shows one branch deeply) and `siblings` (which shows only headlines).

### Files to modify
- `public-ui/orchestrator/prompts/cross_check.md` — NEW prompt
- `public-ui/orchestrator/context.py` — `all_branches_shallow` layer
- `public-ui/orchestrator/run_types.py` — cross_check config
- `public-ui/orchestrator/tools.py` — cross_check tool set
