---
name: parma-ui-audit
description: Walk through the parma frontend (localhost:3100) as a researcher and produce a UI improvement list. Use when the user wants a fresh audit of the UI, a sanity check after frontend changes, or repro of a workflow rough edge. Output is a stacked severity × surface list, not a checklist report.
allowed-tools: Bash, Read
argument-hint: "[persona] [question_or_run_url]"
---

# parma-ui-audit

Play the role of a real researcher using parma, not a QA script. The goal is
to **surface friction**, not tick boxes. Output should read like field notes
from someone trying to get work done.

## Before touching the browser

Run these in parallel:

1. `cat /Users/brian/code/rumil/parma/.env.local` — confirm `NEXT_PUBLIC_API_URL`. Default is `http://localhost:8009`.
2. `lsof -i :3100 -i :8009 | head` — confirm both servers are up. If not, tell the user which is missing and stop.
3. If the user gave a URL, extract `project` and `q` params so you know what data exists.

**Model gotcha:** the API serves from whatever environment started it. Default
is opus (`claude-opus-4-7`). Haiku only kicks in with `RUMIL_TEST_MODE=1` or
`RUMIL_SMOKE_TEST=1`. Dispatching through the chat UI has **no model
override** — if the user said "haiku only", plan around this rather than
assuming you can toggle it.

## Browser tools

All chrome MCP tools are deferred — load with ToolSearch first:

```
ToolSearch "select:mcp__claude-in-chrome__tabs_context_mcp,mcp__claude-in-chrome__find,mcp__claude-in-chrome__read_page,mcp__claude-in-chrome__navigate,mcp__claude-in-chrome__computer"
```

Prefer `find` + `ref` over raw screen coordinates — screenshots come back at
non-obvious resolutions and coordinate math goes wrong. Use `computer` with
`ref` from a prior `find` call.

## Pick a persona

If the user named one, use it. Otherwise pick whichever best matches the
question they sent. At least one full loop per audit.

- **New user** — lands on home, tries to create a workspace, types first
  question, expects something to happen. Surfaces: empty state, create flow,
  "what do I do now?".
- **Skeptical reviewer** — given a specific run_id, wants to judge whether to
  trust the output. Opens trace, inspects LLM exchanges, checks what model
  ran, what was in the prompt, what tools were called. Surfaces: trace
  legibility, model transparency, prompt inspection affordances.
- **Power user** — already has a workspace, wants to dispatch specific calls,
  pin claims, navigate between views. Surfaces: chat tool surface, view
  transitions, keyboard/shift-click affordances, panes interactions.

## Views to touch

`?view=` modes: `panes`, `article`, `vertical`, `sections`, `sources`,
`trace`. Don't grind through all of them — use whichever the persona would.
Always test **back/forward** at least once; URL-as-state is a real feature
and easy to regress.

## Chat

If the persona uses chat, always:

- Ask it "what's in view and what can you do?" — tests contextual awareness.
- Read the tool schemas it advertises. Cross-check against what empty-state
  hints / slash-command chips say. Mismatches are common.
- If you try to dispatch a call, note what parameters are exposed (model?
  budget? specific call type?).

## Output format

Group by **surface**, order within each group by **severity**. Two lines max
per item — one for the observation, one for the suggested fix (if obvious).

Top-level sections in roughly this order (drop any that had nothing):

- **Trace view** (usually highest-impact for researchers)
- **Dispatch / chat tool surface**
- **Home / workspace list**
- **View-specific** (panes, article, etc.)
- **Navigation / layout**
- **Minor** (polish, emoji/aesthetic, label clarity)

Call out what's **working well** too — a one-line positives section near the
end. Skill users should know which surfaces not to touch.

## Things to actively look for

Learned from past audits — check whether these are still true, don't assume:

- Are run labels in the trace index driven by `call_type` or are they all
  "chat"?
- Is the model name surfaced anywhere in trace view? (PARAMS, per-exchange
  header, run metadata)
- Do "scratch" / `chat-persist-*` workspaces clutter the home list?
- Does post-create flow navigate into the new workspace, or just return to
  the list?
- Are slash-command chips in chat consistent with the empty-state hint?
- Do claim inline metric bars have a legend / tooltip?
- Do "shift-click to pin" style hints correspond to visible affordances?

## Scope discipline

- Don't dispatch real opus/sonnet runs unless the user OK'd it — audits
  shouldn't spend money.
- Don't modify data through the UI (no delete, no share, no publish)
  unless the user asks.
- If you notice a likely bug (not just a UI gap), flag it separately — don't
  bury it in the polish list.
