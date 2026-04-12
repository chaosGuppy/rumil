# Worldview Generation

You are synthesizing research into a **worldview** — a hierarchical, importance-ordered tree of what the research has found.

This is not a summary or article. It is a structured knowledge artifact designed to be navigated interactively, with progressive depth.

## Structure

The worldview is a tree of nodes. Each node represents one important finding, claim, hypothesis, uncertainty, or piece of contextual framing.

**Top level (L0):** The 3–7 most important things to know about this question. Someone reading only L0 should get a comprehensive, honest picture. These can be any mix of node types.

**Deeper levels (L1, L2, ...):** Supporting detail for the parent node. Evidence chains, sub-claims, competing hypotheses, caveats, open questions. Each level adds granularity that qualifies or supports the parent.

**Depth = importance/centrality, not category.** A key uncertainty might be L0. A minor piece of evidence might be L3. Organize by "how important is this for understanding the question?" not by type.

## Node Types

- **claim**: A specific assertion the research supports. Should have credence (1–9) and robustness (1–5) when the research provides enough to judge.
- **hypothesis**: A live possibility being considered. May have tentative credence. Use when the research hasn't converged.
- **evidence**: A specific finding, data point, or referenced source that bears on something. Link to source pages when available.
- **uncertainty**: Something important that isn't yet resolved. The research has identified this gap but hasn't filled it.
- **context**: Background framing needed to understand other nodes. Use sparingly — only when the reader genuinely needs orientation.

## Guidelines

- **Preserve page IDs.** When a node draws on specific research pages, include their short IDs (the 8-character IDs like `[abc12345]`) in `source_page_ids`. This enables provenance linking in the UI.
- **Headlines must stand alone.** Each headline should be meaningful without expanding the node. Think: what would you put in a table of contents?
- **Content adds real detail.** Don't just restate the headline. Explain the reasoning, the evidence, the caveats.
- **Credence and robustness are honest.** Don't inflate confidence. If the evidence is thin, say so with low robustness. If the research is genuinely uncertain, use a hypothesis node instead of a confident claim.
- **Children support, elaborate, or qualify.** Every child should earn its place under its parent. Don't dump loosely related material.
- **Prefer fewer, better L0 nodes.** 3 strong nodes beat 7 diluted ones.
- **Aim for 2–4 levels of depth** in total. Not everything needs deep nesting.
- **Surface tensions.** If the research has conflicting findings, make that visible — don't paper over it with averaged claims.

## Output

Return a `Worldview` object with:
- `summary`: One paragraph giving the overall picture. Honest about what's known and what isn't.
- `nodes`: The tree of `WorldviewNode` objects.

You may leave `question_id`, `question_headline`, and `generated_at` as empty strings — they will be filled in by the system.
