---
name: rumil-show
description: Show a rumil question — its research subtree, embedding-based neighbors across the workspace, and recent calls that have run against it. Takes a short (8-char) or full question ID. Use whenever the user asks to view, inspect, or look at a question.
allowed-tools: Bash
argument-hint: "<question_id> [--depth N] [--no-neighbors] [--no-calls]"
---

# rumil-show

> **Under the hood:** this skill calls `rumil_skills.show_question`,
> which combines `rumil.summary.build_research_tree` (subtree),
> `rumil.context.build_embedding_based_context` (workspace neighbors —
> same builder used by real rumil calls), and a direct `calls`-table
> read for recent calls. Read-only; no dispatch function involved.

Renders a full picture of one question: its subtree (sub-questions, claims,
judgements), the most relevant pages from the rest of the workspace
(embedding-based), and the most recent calls that have targeted it.

This is the primary "pull context into CC" skill. After running it, Claude has
everything needed to discuss the question, spot problems, or decide what to
dispatch next.

```!
setopt no_glob 2>/dev/null; set -f; PYTHONPATH=.claude/lib uv run python -m rumil_skills.show_question $ARGUMENTS
```
