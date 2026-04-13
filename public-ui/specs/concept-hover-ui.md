# Concept Hover UI

## Context

Concept nodes were just added to the data model (node_type: "concept"). They should render differently from other nodes — not as full cards in the tree, but as lightweight inline references that show a hover popover with the definition.

## What exists

- `node_links` table with `supports`, `opposes`, `depends_on`, `related` link types
- `concept` node type in the schema and types
- CSS variables: `--node-concept` / `--node-concept-bg` colors
- `NodeTypeLabel.tsx` has a "concept" entry
- `WorldviewNode` type has `id` and `links_out`/`links_in` (optional)

## What to build

### 1. Concept reference detection

When a concept node exists in the workspace, its headline should be detected in other nodes' content and rendered as a hoverable reference. Similar to how `TextWithNodeRefs` detects 8-char hex IDs, but matching concept headlines.

The API needs to return concepts for the workspace:
- `GET /api/workspaces/{name}/concepts` — returns all concept nodes (id, headline, content)
- Or include them in the tree response as a separate field

### 2. ConceptRef component

A small inline component (like `SourcePill`):
- Renders as subtle underlined/dotted text inline with content
- On hover: popover with concept headline + content (the definition)
- Styled with `--node-concept` color
- Should not interrupt reading flow — lighter than SourceBadge popovers

### 3. Integration points

- `WorldviewNode.tsx` — concept nodes themselves render differently (compact, no expand arrow, maybe just headline + content inline)
- `ArticleView.tsx` — concept references in node content get the hover treatment
- `VerticalView.tsx` — same

### Technical approach

- Fetch concepts once per workspace (React context or lifted state)
- Build a regex from concept headlines
- Replace matches in content text with `<ConceptRef>` components
- Popover positioning: same pattern as `SourceBadge` (useRef + absolute positioning)

### Design

- Concept nodes in the tree: render as a compact "definition" style — just the headline and content, no credence/robustness, no expand
- Concept references: dotted underline in `--node-concept` color, hover shows definition
- Keep it lightweight — concepts should be helpful, not noisy

### Files to modify
- `public-ui/serve.py` — concepts endpoint
- `public-ui/src/components/ConceptRef.tsx` — NEW
- `public-ui/src/components/WorldviewNode.tsx` — special rendering for concept nodes
- `public-ui/src/components/ArticleView.tsx` — concept refs in content
- `public-ui/src/app/globals.css` — concept styles
