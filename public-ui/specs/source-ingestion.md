# Source Ingestion Pipeline

## Context

Original rumil has a full ingest pipeline: create a Source page from a URL, run extraction rounds that turn the source into considerations on a target question. Public-UI has a sources table with metadata but no automated extraction. The `/ingest` slash command exists in the chat but does nothing.

## What to build

### 1. Ingest endpoint

`POST /api/workspaces/{name}/ingest`

```json
{
  "url": "https://arxiv.org/abs/...",
  "target_node_id": "abc123",  // optional — which node to extract evidence for
  "title": "..."  // optional — auto-extracted if omitted
}
```

Flow:
1. Fetch the URL content (use httpx or similar)
2. Extract readable text (strip HTML, handle PDFs)
3. Create a Source record in the sources table
4. If target_node_id provided: run an extraction step — LLM reads the source content and produces evidence/claim nodes under the target, with source_ids pointing back to the Source record

### 2. Extraction as an orchestrator run

The extraction step is essentially an explore run with the source content injected into context. Could be:
- A new run type `ingest` with its own prompt
- Or a regular explore run with source content prepended to the user message

The ingest prompt should guide the model to:
- Extract specific, falsifiable claims from the source
- Create evidence nodes (not claims) when reporting source findings
- Set appropriate credence/robustness (evidence from a source starts at R3+ since it's grounded)
- Link evidence to the source via source_page_ids
- Note limitations or caveats from the source as uncertainty nodes

### 3. Chat integration

Wire the `/ingest` slash command:
```
User: /ingest https://arxiv.org/abs/2301.00000 --for abc123
Model: calls ingest tool → source created, extraction run started
```

Add an `ingest_source` chat tool.

### 4. Source content rendering

The SourceDrawer component already exists for viewing sources. After ingestion, it should show the extracted nodes linked back from the source.

### Files to modify
- `public-ui/serve.py` — ingest endpoint, source fetching, chat tool
- `public-ui/orchestrator/prompts/ingest.md` — NEW extraction prompt
- `public-ui/orchestrator/run_types.py` — ingest run type
- `public-ui/src/components/SlashCommands.tsx` — wire /ingest

### Content extraction

For v1, just use httpx + basic HTML stripping (BeautifulSoup or similar). For PDFs, could use a library or just skip PDFs initially. The important thing is getting text content into the system, not handling every format.
