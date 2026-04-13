# Web Research Tool for Orchestrator

## Context

Original rumil has a full web_research call type with server tools + scraping. Public-UI has nothing — the orchestrator can only work with what's already in the tree. This means it generates plausible-sounding claims but can't verify them against actual sources.

This is the single biggest quality gap between the two systems. The orchestrator produces nodes like "ISO/IEC JTC 1/SC 42 is dominated by industry representatives" — but is that actually true? Without web access, it's generating from training data with no grounding.

## What to build

### 1. Web search tool for the orchestrator

Add a `web_search` tool that the orchestrator can call during runs:

```python
{
    "name": "web_search",
    "description": "Search the web for evidence, data, or sources relevant to the branch.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "purpose": {"type": "string", "description": "What you're looking for and why"},
        },
        "required": ["query", "purpose"],
    },
}
```

### 2. Implementation options

**Option A: Anthropic's built-in web search tool** (simplest)
- Use `type: "web_search_20250305"` in the tools list
- The model handles search + retrieval natively
- No custom backend needed
- Limitations: less control over what gets searched

**Option B: Custom search backend**
- Call a search API (Google, Brave, Serper) from the tool executor
- Fetch + extract content from top results
- More control, but more infrastructure

**Option C: Hybrid — web search for discovery, source ingestion for depth**
- Web search finds relevant URLs
- A separate `ingest_source` tool fetches the full page, creates a Source record, and extracts claims as evidence nodes
- This mirrors rumil's ingest pipeline

Recommend starting with Option A (built-in) and adding Option C later.

### 3. Source creation from web results

When the orchestrator finds useful web content, it should be able to create source nodes:
- Create a Source record (title, URL, abstract)
- Link evidence nodes to the source via source_page_ids
- This gives provenance — "this evidence came from this URL"

### 4. Prompt guidance

Update explore prompt: when adding evidence nodes, prefer grounded findings over training-data assertions. Use web_search to verify claims when possible.

### 5. Tool set updates

Add web_search to the explore tool set (not evaluate — evaluate doesn't add content):
```python
"explore": [ADD_NODE, UPDATE_NODE, RELEVEL_NODE, LINK_NODES, WEB_SEARCH, SUGGEST_CHANGE, INSPECT_BRANCH],
```

### Files to modify
- `public-ui/orchestrator/tools.py` — web_search tool definition + execution
- `public-ui/orchestrator/run_types.py` — add to explore tool set
- `public-ui/orchestrator/prompts/explore.md` — guidance on using web search
- `public-ui/serve.py` — source creation from web results
