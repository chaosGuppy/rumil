---
description: Use this skill whenever the user asks you to resolve, fix, handle, or deal with merge conflicts — after a failed `git merge`, `git rebase`, `git cherry-pick`, `git stash pop`, or `git pull`. Trigger on phrases like "resolve the conflicts", "fix the merge", "finish the rebase", "handle these conflicts", "the merge blew up", or when the user shares output containing `<<<<<<<`, `=======`, `>>>>>>>` markers or files with status `UU`/`AA`/`DD`. Also trigger proactively if you encounter merge conflicts while performing some other git operation the user asked for.
---

# Resolving Merge Conflicts

The goal is to resolve every conflict you can resolve with **high confidence**, leave ambiguous ones alone, and give the user a clear report of both. Never guess when the user could tell you in seconds — but don't ask about things where the correct fix is obvious, either.

## Workflow

### 1. Enumerate the conflicts

List every file with an unresolved conflict:

```
git status --short
git diff --name-only --diff-filter=U
```

Also note what operation is in progress (merge, rebase, cherry-pick) — `git status` tells you this, and it changes how you read the history. During a rebase, "ours" is the branch being rebased *onto* and "theirs" is the commit being replayed; during a merge it's the opposite. Getting this wrong will flip your understanding of every conflict.

### 2. Understand each conflict before proposing anything

For each conflicted file, actually dig in. Surface-level pattern matching on conflict markers is exactly how you end up silently dropping someone's work. Read:

- **The conflicted file itself**, including the markers. Look at the full surrounding function/block, not just the hunks.
- **The commits on each side.** The two most useful commands:
  - `git log --merge --oneline -- <path>` — commits touching this file from both sides since the merge base.
  - `git log -p <base>..HEAD -- <path>` and `git log -p <base>..MERGE_HEAD -- <path>` (substitute `REBASE_HEAD`/`CHERRY_PICK_HEAD` as appropriate) to see the actual diffs.
- **The commit messages.** They often explain *why* each side made the change, which is what you need to judge whether the changes compose or genuinely disagree.
- **The broader file.** If a conflict is in an import block or a function signature, check whether callers elsewhere in the repo care.

If you can't tell what a side was trying to do, that's valuable information — it becomes the "source of uncertainty" you'll report.

### 3. Score each conflict 0–10

For each conflict, write down a proposed resolution and a confidence score. Be honest; inflated scores defeat the point.

- **10** — You are completely confident. The changes are orthogonal and both apply cleanly (e.g. both sides added a different import, or renamed different things). Or one side is a clear strict superset of the other. Or the conflict is purely in generated/lock-file content with an obvious "take the newer version" answer that matches how the rest of the repo handles it.
- **7–9** — You have a well-supported hypothesis but some residual doubt — e.g. the correct merge is clear structurally but you'd want to double-check a caller, or the commit messages are terse.
- **4–6** — You can see plausible resolutions but the sides genuinely disagree on intent and you'd be picking one interpretation over another.
- **0–3** — You don't really know what either side was going for, or applying either change alone seems likely to break something you can't see.

Think hard about what could make a 10 actually wrong. Common traps:
- Both sides added entries to a list/dict/enum that look orthogonal but depend on ordering, ID uniqueness, or an invariant elsewhere.
- A "trivial" formatting conflict sits inside a block where one side also changed the logic.
- Lock files and generated files look mechanical but can encode version constraints — only score 10 if you understand the regeneration story for this repo.

When in doubt, drop a point.

### 4. Apply all the 10s automatically

Fix every conflict you scored 10, without asking. After editing, run `git add <path>` for each fully-resolved file so the index reflects the resolution. Do **not** run `git commit`, `git merge --continue`, `git rebase --continue`, etc. — leave that to the user, who may want to inspect or add more changes first.

Leave files with scores below 10 in their conflicted state (markers intact, not `git add`ed). Don't half-resolve them.

### 5. Report

Structure the report like this:

**Fixes applied** (one short line each):
- `path/to/file.py` — one-sentence description of the resolution. Keep it tight; the user can read the diff.

**Conflicts left for you** (one block per conflict, more detailed):
- `path/to/file.py` — confidence: N/10
  - **What each side did:** brief summary of both sides' intent, with commit references if helpful.
  - **Proposed resolution:** what you'd do if forced to pick, and why.
  - **Source of uncertainty:** the specific thing you couldn't verify — e.g. "I don't know whether callers in `foo.py` expect the old signature", "both sides reworded the same error message and I can't tell which wording is canonical", "the rebased commit predates a refactor on main and I'm not sure which version represents current intent".

If there are no sub-10 conflicts, say so explicitly and remind the user to review the staged fixes before continuing the merge/rebase.

## Notes

- `git checkout --ours <path>` / `--theirs <path>` are fine shortcuts when a whole file should come from one side and you're confident — but remember ours/theirs is inverted during rebase. Prefer editing the file directly when only part of it is in conflict.
- Never run `git merge --abort`, `git rebase --abort`, or any `--skip` variant unless the user explicitly asks. Those discard work.
- If the repo has post-merge formatting or regeneration hooks (e.g. a lock file regenerates from a manifest), note it in the report rather than trying to run the regeneration yourself unless that's obviously part of what the user asked for.
