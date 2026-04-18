---
allowed-tools: Bash(gh pr view:*), Bash(gh pr diff:*), Bash(gh pr list:*), Bash(gh api:*), Bash(gh search:*), Bash(git:*)
description: Address the latest review comment on the current PR — fix real issues, explain non-issues, and treat reviewer confusion as a signal that the code itself may need clarifying.
disable-model-invocation: false
---

Address the latest review comment on the current PR.

Steps:

1. Identify the current PR from the current branch (`gh pr view --json number,url,headRefName`).
2. Fetch the latest review comment(s) on that PR. "Latest" means the most recent review (`gh api repos/{owner}/{repo}/pulls/{n}/reviews`) and its inline comments (`gh api repos/{owner}/{repo}/pulls/{n}/comments`), plus any issue-level comments (`gh api repos/{owner}/{repo}/issues/{n}/comments`) posted after the last review. Take whichever cluster is newest. If there are multiple distinct points raised, handle each one.
3. For each point, decide honestly whether it is a **real issue** or a **non-issue**. Do not default to agreement — reviewers are sometimes wrong. But also do not default to disagreement — think carefully.
4. For each **real issue**: fix it. Make the minimal change that addresses the concern. Do not expand scope.
5. For each **non-issue**: do NOT change the behavior the reviewer flagged. Instead:
   a. Carefully explain to the user, in the chat, why this is a non-issue. Cite the specific code and reasoning. Be concrete — "the reviewer thinks X, but actually Y because Z (see file.py:123)".
   b. **The fact that the reviewer was confused is itself a signal.** A competent reviewer looked at this code and got it wrong. That usually means the code is confusing, not that the reviewer is bad. Strongly consider one of:
      - Adding a short comment on the confusing line explaining the non-obvious WHY (only the WHY — see CLAUDE.md comment guidance).
      - A small local refactor that makes the intent obvious (renaming a variable, extracting a well-named helper, reordering for clarity).
   c. Only skip the clarifying change if the confusion was clearly the reviewer's fault (e.g. they misread, didn't look at the surrounding context, or made a factual error unrelated to how the code reads).
6. Report back:
   - What you fixed, and where.
   - What you pushed back on, with the reasoning.
   - What clarifying comment/refactor you made (or why you decided none was needed) for each non-issue.

Do not post a reply on the PR yourself. The user will review your changes and respond to the reviewer.

Notes:
- Use `gh` for all GitHub interactions, not WebFetch.
- If there are no review comments yet, say so and stop.
- Keep changes tightly scoped to what the reviewer raised. If you spot unrelated issues while in the area, mention them to the user — don't silently fix them.
