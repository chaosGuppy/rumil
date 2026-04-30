---
name: rumil-forks
description: Edit and re-fire a captured LLM exchange to see how the model would respond under different conditions — tweaked system prompt, edited message stack, different tool list, different model, different temperature. Side-effect-free (no tool execution, no workspace mutation, no trace recording). Use when the user wants to probe model behavior on an existing exchange — "what if the system prompt said X", "would the model still call that tool if Y was different", "is this exchange stable across samples", "try sampling at higher temp". Admin-only feature; results persist to exchange_forks for side-by-side comparison in the UI. Subcommands: show / fire / list. Pair with /rumil-trace to find exchange_ids.
allowed-tools: Bash, Read, Write
argument-hint: "<show|fire|list> <exchange_id> [--overrides PATH] [--samples N] [--prod]"
---

# rumil-forks

Edit-and-rerun for a captured LLM exchange. The base exchange is the
canonical starting point; overrides replace specific fields (`system_prompt`,
`user_messages`, `tools`, `model`, `temperature`, `max_tokens`); N samples
fire in parallel; results persist to `exchange_forks` and render alongside
the original in the trace UI.

**Side-effect-free.** Tool calls returned by the model are stored as data,
not executed. No pages, links, or mutation events are written. No trace
events are recorded against the original call. Forks live entirely outside
the staged-runs visibility model.

## When to use

- "Fire that exchange again with the system prompt tweaked to ..."
- "Sample this 5 times at temp 1 to see if there's variance"
- "What if `create_claim` wasn't available — would the model use a different tool?"
- "Try this exchange with Sonnet instead of Opus"
- "Edit the user message to ask for X instead and see what changes"
- "Did the model fixate on this framing? Try rephrasing"

For "actually run the agent loop from this point" (multi-turn, with tool
execution, in a staged run), this skill is **not** the right tool — that
expansion isn't built yet. Forks are single-turn-only: one LLM call, one
response, no tool execution.

## Workflow

1. **Find the exchange_id.** Usually from `/rumil-trace <call_id>` output —
   each `LLMExchangeEvent` shows its UUID. Or from a UI trace URL where the
   panel exposes the id.

2. **Run `show` first**, even if the user described the change abstractly.
   You need to see the current system prompt, message stack, and available
   tools before deciding what to override:
   ```
   uv run python scripts/exchange_forks.py show <exchange_id>
   ```

3. **Build a minimal overrides file.** Only include fields that change —
   anything omitted inherits from the base. Write to a temp path under
   `.scratch/`. JSON shape:
   ```json
   {
     "system_prompt": "...",
     "user_messages": [{"role": "user", "content": "..."}],
     "tools": [{"name": "...", "description": "...", "input_schema": {...}}],
     "model": "claude-sonnet-4-6",
     "temperature": 0.7,
     "max_tokens": 4096
   }
   ```
   `tools` is a **full replacement** — to remove a tool, omit it; to add or
   edit one, include the desired Anthropic tool dict. Prefer minimal diffs
   over full rewrites so the diff intent is legible in the UI later.

4. **Fire and report:**
   ```
   uv run python scripts/exchange_forks.py fire <exchange_id> \
       --overrides .scratch/forks/foo.json --samples 3
   ```
   The script prints each sample's response inline, total cost, and the
   fork ids. Surface the fork ids and cost to the user. If they want to
   view the side-by-side comparison, point them at the trace URL — the
   admin fork panel for the base exchange shows all variants.

5. **List prior attempts** when iterating:
   ```
   uv run python scripts/exchange_forks.py list <exchange_id>
   ```
   Groups by `overrides_hash` so each unique config shows up as a row with
   its sample count and which fields differ.

## Common patterns

- **"What if the system prompt said X?"** — 1 sample, default temp.
- **"Is the model unstable here?"** — no overrides except `temperature: 1.0`,
  4–8 samples. Watch for content drift across samples.
- **"What if tool X was unavailable?"** — copy the tools list from `show`
  output, drop tool X, save as overrides.
- **"Try several different framings"** — multiple `fire` calls with
  different override files; the UI groups them as separate variant columns.
- **"Compare models"** — same overrides but different `model`. Each model
  is its own column in the UI.

## Don'ts

- **Don't promote casually.** Forks never write to canonical prompt files
  (`src/rumil/prompts/*.md`). If a fork wins, surface that to the user as
  an explicit prompt-edit suggestion they can act on — don't auto-edit.
- **Don't fire blind.** Always run `show` before `fire` to make sure your
  override is actually a meaningful diff.
- **Don't run on prod without explicit user direction.** Default to local;
  pass `--prod` only if the user said so.

## Invocation

```!
setopt no_glob 2>/dev/null; set -f; uv run python scripts/exchange_forks.py $ARGUMENTS
```

## Cost

Each sample is a fresh API call with no prompt-cache hit (overrides bust
the cache). For long system prompts × many samples × Opus, this adds up
fast. If sampling >3 times on a long-context exchange, mention the
estimated cost to the user before firing.
