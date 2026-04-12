# View Modes — Implementation Spec

The worldview tree can be rendered in three view modes. The user switches
between them (URL param `?view=panes|article|vertical`). All three render
the same `Worldview` data from the API (`GET /api/workspaces/{name}/tree`).
The stacked panes mode is already built. This doc specifies the article
and vertical modes for another session to implement.

## Shared infrastructure

All modes share:
- `WorldviewNode` type from `src/lib/types.ts`
- `CredenceBadge` component for credence/robustness display
- `NodeTypeLabel` component for type indicators
- `fetchWorldview()` from `src/lib/api.ts`
- CSS variables in `globals.css` (node type colors, active tints, fonts)
- The chat panel (appears alongside any view mode)

**View mode switcher**: Add a small control in the page header (or top of
the first pane) with three options: Panes / Article / Vertical. Use URL
search param `?view=` so it's deep-linkable. Default is `panes`.

The `page.tsx` should conditionally render `StackedPanes`, `ArticleView`,
or `VerticalView` based on the param.

## Article View

A single scrollable column rendering the worldview as a long-form document.
Think: a well-typeset research brief.

### Layout

```
┌─────────────────────────────────────────┐
│  [question headline]                     │
│  [summary paragraph]                     │
│                                          │
│  ─── L0 Nodes as Sections ───           │
│                                          │
│  ## Governance gaps  [claim C7/R4]       │
│  [full content paragraph]                │
│                                          │
│  ### L1 children rendered inline         │
│  [evidence] Regulatory frameworks...     │
│  [claim] Standards bodies...             │
│                                          │
│  ### L2 children indented further        │
│  [evidence] Nuclear precedent...         │
│                                          │
│  ▸ Supplementary (L3+) — collapsed       │
│    [collapsed section, click to expand]  │
│                                          │
│  ─── next L0 section ───                │
│  ## Interpretability limits [uncertainty]│
│  ...                                     │
└─────────────────────────────────────────┘
```

### Rules

- **L0 nodes** become top-level sections (`<h2>`) with full content
- **L1 nodes** render inline under their parent as subsections (`<h3>`)
  with full content
- **L2 nodes** render as indented paragraphs with smaller headings
- **L3+ nodes** (supplementary) are collapsed by default in a
  "Supplementary" disclosure section at the end of each L0 section.
  Click to expand. This is the only interactive element.
- Each section heading shows the node type label and credence badge
- A **table of contents** sidebar (fixed left, like the removed ToC)
  lists L0 nodes as nav anchors. Only visible on wide viewports.
- Provenance indicators (source count) shown inline
- **No panes, no horizontal scroll** — single column, max-width ~720px,
  centered

### Implementation notes

- Create `src/components/ArticleView.tsx`
- Recursively render the worldview tree with depth determining heading
  level and indentation
- Use the `importance` field on nodes for the supplementary cutoff
  (importance >= 3 → supplementary)
- Supplementary sections use `<details>/<summary>` for native
  collapse/expand (no state needed)
- Style with the existing `worldview-prose` class for body text
- Add a `toc` sidebar back (removed from stacked panes, useful here)
  — only render if viewport > 1024px

### Interaction

- Clicking a node headline should fire the same `onFocus` callback
  as other views (for chat cross-linking)
- The URL should encode scroll position or focused section for
  deep-linking: `?view=article#section-2`
- Clicking a source page ID should trigger `onNodeRef` to highlight
  in the tree

## Vertical View

A single scrollable column with indentation showing tree depth.
Think: an expandable outline.

### Layout

```
┌─────────────────────────────────────────┐
│  [question headline]                     │
│  [summary paragraph]                     │
│                                          │
│  [claim] Governance gaps  C7/R4  L0      │
│  [full content]                          │
│    ▸ 3 children                          │
│                                          │
│  [uncertainty] Interp limits  L0         │
│  [full content]                          │
│    ▾ 3 children (expanded)               │
│    │ [evidence] SAE features  C8/R4  L1  │
│    │ [full content]                      │
│    │   ▸ 2 children                      │
│    │ [hypothesis] New approaches  C4  L1 │
│    │ [full content]                      │
│    │   ▸ 2 children                      │
│    │ [claim] Necessary not sufficient L1 │
│    │ [full content]                      │
│                                          │
│  [claim] Incentive misalignment  C7  L0  │
│  ...                                     │
└─────────────────────────────────────────┘
```

### Rules

- Renders the full tree in a single column
- Each node shows: type label, headline, credence badge, importance level
- L0 nodes always show full content
- L1+ nodes show content when expanded
- Expand/collapse via chevron or click on headline
- Indentation increases with depth (left padding or left border)
- Each depth level gets a subtle left border with the depth-cycling
  color (reuse `--active-0` through `--active-4`)
- **No panes** — depth is shown through nesting, not side-by-side columns

### Implementation notes

- Create `src/components/VerticalView.tsx`
- Use local state for which nodes are expanded: `Set<string>` of node
  paths (e.g., "0", "0.1", "0.1.2")
- URL-encode expanded set: `?view=vertical&expanded=0,0.1,2`
- Reuse `WorldviewNodeCard` but adapted for vertical layout — remove
  the pane-related props, add indent styling
- Or create a simpler `VerticalNodeCard` that's more compact

### Interaction

- Clicking a headline toggles expand/collapse AND fires `onFocus`
- Expanded state persists in URL params
- Works well on narrow viewports (no min-width requirement)
- Keyboard: arrow keys to navigate between nodes, Enter to
  expand/collapse (stretch goal)

## CSS tokens to add

Both views should reuse existing CSS variables. New tokens if needed:

```css
--article-max-width: 720px;
--article-toc-width: 200px;
--vertical-indent: 24px;
--supplementary-opacity: 0.6;
```

## Testing

- Verify all three views render the same data from the API
- Check that chat `onNodeRef` cross-linking works in all views
- Check that `onMessageSent` refresh updates all views
- Check URL deep-linking round-trips for each view mode
- Check narrow viewport (< 768px) — article and vertical should work,
  panes should gracefully degrade or redirect to vertical
