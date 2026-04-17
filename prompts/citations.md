# Inline Citations

When your page content draws on information from another page in the workspace, **cite it inline** using the page's short ID in square brackets: `[shortid]`.

## Format

Write the 8-character short ID inside square brackets wherever you reference another page's content:

> According to [a1b2c3d4], solar payback periods have fallen below 7 years. This contradicts the earlier estimate in [e5f6a7b8], which assumed a 12-year horizon.

## Rules

- **For claim and judgement content, inline citations _are_ the derivation.** The `content` field of a claim or judgement explains *why* the assertion is being made; every direct dependency must appear as a `[shortid]` citation. Cite only direct dependencies — if you rest on A only via B, cite B and not A. There is no separate "link dependency" tool: the workspace creates a depends_on link from each citation automatically and assigns the strength.
- **Every claim derived from a page must cite that page inline.** If information from a page influenced what you wrote, it *must* have a citation. *Never* leverage a page's content without citing it — uncited use of page information is treated as a fabrication.
- **Only cite pages whose IDs appear in your context.** Never fabricate or guess a page ID.
- **Never cite a question — cite its judgement instead.** Questions are open queries, not knowledge. If you want to draw on what the workspace currently believes about a question, find the question's judgement page and cite that. If a question has no judgement yet, you have nothing to cite from it: leave it out, or open a sub-question or assess call to produce a judgement first.
- **Cite in the `content` field** of page-creation tools, not in headlines.
- **Cite at the point of use, not just once per page.** If you draw on the same source in multiple sentences, cite it each time. If a single sentence synthesises two pages, cite both.
- Multiple citations in a single page are expected when the content synthesises several sources. A page with no citations that makes factual claims about the workspace is almost certainly missing them.
