# Plan Workspace Updates

You're applying web research findings to a workspace. The work splits into two phases.

## Phase 1: Update directly-affected claims

For every claim that's **directly affected** by the findings, spawn a `claim-updater` subagent. You update *all* of them — directly-affected claims don't count against your budget.

A claim is directly affected when the findings contradict, refine, or bolster its specific content. Transitively affected claims (wrong only because they lean on another affected claim) are not your concern in this phase — they'll be handled separately.

Use `explore_page` to navigate the workspace graph and identify which claims the findings directly bear on.

Each subagent prompt must include:

1. The claim's page ID (8-char short ID).
2. The **full text** of the relevant findings, with source URLs. The subagent can't see your conversation — everything it needs has to be in the prompt you write.
3. Specific guidance on how the claim should be updated, if the direction isn't obvious from the findings.

The subagent uses `create_claim` with the old claim's ID in `supersedes` and the URLs in `source_urls` to produce a properly grounded replacement.
