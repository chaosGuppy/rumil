---
name: rumil-page
description: Inspect a single rumil page by short ID — full content, provenance (which call created it), epistemic scores, superseded state, and all incoming/outgoing links with target headlines resolved. Use whenever you see a non-question page's short ID (a claim, judgement, concept, view, view_item, view_meta, wiki page, source) in a trace, punch list, or conversation and want to read its actual content and how it connects. Complements rumil-show which is question-specific.
allowed-tools: Bash
argument-hint: "<page_id> [--no-links] [--content-limit N]"
---

# rumil-page

Single-page inspector. Takes a full or short (8-char) page ID and
dumps everything a reviewer typically needs:

- core identity (type, workspace, layer, headline, creation time)
- provenance (which model/call produced it, via which call type)
- epistemic scores (credence, robustness — claims and judgements only)
- superseded state + pointer
- full abstract + content (content truncated by default at 4000 chars;
  pass `--content-limit 0` for no limit)
- **outgoing links** with target page headlines + link reasoning
- **incoming links** with source page headlines + link reasoning

This is the skill to reach for when `rumil-trace` or `rumil-review` or
`rumil-show` surfaces a claim short ID like `be6d1a1d` and you want to
know what it actually says, not just the headline.

Use `rumil-show` for questions (it gives a subtree + embedding
neighbors + recent calls). Use `rumil-page` for everything else —
claims, judgements, concepts, views, view_items, view_meta, wiki
pages, sources, summaries. Either one works on a question ID too;
`rumil-show` just has more question-specific surface.

```!
setopt no_glob 2>/dev/null; set -f; PYTHONPATH=.claude/lib uv run python -m rumil_skills.show_page $ARGUMENTS
```
