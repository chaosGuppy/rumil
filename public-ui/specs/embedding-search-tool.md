# Embedding Search Agent Tool

## Context

Original rumil uses embedding-based vector similarity search to surface relevant pages from anywhere in the workspace, filling full-page and summary tiers within configurable char budgets. Public-UI has no embedding infrastructure — the orchestrator only sees its scoped branch context.

The data flow diagram envisions embedding search as an **agent tool** (not pre-loaded context). The orchestrator decides when to look beyond its branch and pulls in relevant nodes on demand.

## What to build

### 1. Embedding infrastructure

Compute and store embeddings for node headlines + content:

**Option A: Anthropic Voyager embeddings**
- Call Anthropic's embedding API for each node
- Store embeddings in a new column or table
- Pros: consistent with the rest of the stack
- Cons: API calls for each node, cost

**Option B: Local embeddings (sentence-transformers)**
- Use a lightweight model like all-MiniLM-L6-v2
- Compute locally, no API cost
- Store in SQLite (as JSON arrays or blob)
- Pros: free, fast
- Cons: needs the model downloaded

**Option C: SQLite FTS5 (text search, not semantic)**
- Use SQLite's full-text search instead of embeddings
- No vector math, simpler
- Less semantically aware but much simpler
- The `search_nodes` function already does basic LIKE search

Recommend Option C for v1 (it works and we have it), with Option B as a future upgrade.

### 2. Search tool for orchestrator

```python
{
    "name": "search_workspace",
    "description": (
        "Search the entire workspace for nodes relevant to your current work. "
        "Use when you suspect relevant evidence or claims exist in other branches. "
        "Returns matching nodes with their branch context."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for"},
        },
        "required": ["query"],
    },
}
```

### 3. Integration

Add to orchestrator tool sets (explore + evaluate + cross_check):
- Explore: search for existing evidence before creating new nodes
- Evaluate: check if claims in this branch have supporting/opposing evidence elsewhere
- Cross-check: find related content across branches

### 4. Context enrichment

When the orchestrator finds relevant nodes via search, it can:
- Create `depends_on`, `supports`, or `opposes` links to them
- Reference them in new node content
- Use them to calibrate credence/robustness scores

### Files to modify
- `public-ui/orchestrator/tools.py` — search_workspace tool definition
- `public-ui/orchestrator/tools.py` — tool executor implementation (reuse existing search_nodes)
- `public-ui/orchestrator/run_types.py` — add to tool sets
- `public-ui/orchestrator/prompts/explore.md` — guidance on when to search
