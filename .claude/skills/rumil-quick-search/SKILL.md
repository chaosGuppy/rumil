---
name: rumil-quick-search
description: Quick workspace lookup — checks if rumil already has research on a topic. Use proactively when working on rumil tasks and a topic comes up that might already be covered. Lighter and faster than rumil-search.
allowed-tools: Bash
---

# rumil-quick-search

Fast embedding search returning top-8 pages at abstract detail with
similarity scores. Use this to ground your reasoning in actual workspace
content before making claims about what the research does or doesn't cover.

```!
PYTHONPATH=.claude/lib uv run python -m rumil_skills.quick_search $ARGUMENTS
```
