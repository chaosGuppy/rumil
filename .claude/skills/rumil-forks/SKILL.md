---
name: rumil-forks
description: Edit and re-fire a captured LLM exchange to see how the model would respond under different conditions — tweaked system prompt, edited message stack, different tool list, different model, different temperature, adaptive thinking on/off (Opus 4.7/4.6, Sonnet 4.6). Side-effect-free (no tool execution, no workspace mutation, no trace recording). Use when the user wants to probe model behavior on an existing exchange — "what if the system prompt said X", "would the model still call that tool if Y was different", "is this exchange stable across samples", "try sampling at higher temp", "what if thinking was off". Admin-only feature; results persist to exchange_forks for side-by-side comparison in the UI. Subcommands: show / fire / list. Pair with /rumil-trace to find exchange_ids.
allowed-tools: Bash, Read, Write
argument-hint: "<show|fire|list> <exchange_id> [--overrides PATH] [--samples N] [--prod]"
---

# rumil-forks

Edit-and-rerun for a captured LLM exchange. The base exchange is the
canonical starting point; overrides replace specific fields (`system_prompt`,
`user_messages`, `tools`, `model`, `temperature`, `max_tokens`,
`thinking_off`); N samples fire in parallel; results persist to
`exchange_forks` and render alongside the original in the trace UI.

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
- "What if Opus had thinking turned off — does the answer change?"

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
   Pay attention to `user_messages (N)` — for round-1+ exchanges in an
   agent loop (web_research, ingest, multi-round scout), the stack
   contains the full prior-round assistant turns and can be very large
   (~80k tokens isn't unusual). The `show` output truncates per-message
   previews, but the total drives cost on the fire path.

3. **Check `list` if iterating.** Before firing, see what configs you've
   already tried for this exchange so you don't re-fire identical
   overrides by accident:
   ```
   uv run python scripts/exchange_forks.py list <exchange_id>
   ```

4. **Build a minimal overrides file.** Only include fields that change —
   anything omitted inherits from the base. Write to a temp path under
   `.scratch/`. JSON shape:
   ```json
   {
     "system_prompt": "...",
     "user_messages": [{"role": "user", "content": "..."}],
     "tools": [{"name": "...", "description": "...", "input_schema": {...}}],
     "model": "claude-sonnet-4-6",
     "temperature": 0.7,
     "max_tokens": 4096,
     "thinking_off": true
   }
   ```
   `tools` is a **full replacement** — to remove a tool, omit it; to add or
   edit one, include the desired Anthropic tool dict. Prefer minimal diffs
   over full rewrites so the diff intent is legible in the UI later.

   `thinking_off: true` disables adaptive thinking on models that have it
   on by default (Opus 4.7/4.6, Sonnet 4.6). Useful for asking "would
   the model arrive at the same answer without spending thinking tokens?"
   On models that don't have adaptive thinking (Haiku, older Sonnet), the
   flag is a no-op — leave it omitted.

5. **Fire and report:**
   ```
   uv run python scripts/exchange_forks.py fire <exchange_id> \
       --overrides .scratch/forks/foo.json --samples 3
   ```
   The script prints each sample's response inline, total cost, and the
   fork ids. Surface the fork ids and cost to the user. If they want to
   view the side-by-side comparison, point them at the trace URL — the
   admin fork panel for the base exchange shows all variants.

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
- **"Does thinking change the answer?"** — `{"thinking_off": true}` on Opus
  4.7/4.6 or Sonnet 4.6, 1–3 samples. Compare against the captured
  thinking-on response to see whether deliberation changed anything.

## Don'ts

- **Don't promote casually.** Forks never write to canonical prompt files
  (`src/rumil/prompts/*.md`). If a fork wins, surface that to the user as
  an explicit prompt-edit suggestion they can act on — don't auto-edit.
- **Don't fire blind.** Always run `show` before `fire` to make sure your
  override is actually a meaningful diff.
- **Don't run on prod without explicit user direction.** Default to local;
  pass `--prod` only if the user said so.

## Caveats

- **Tool reconstruction is best-effort.** Exchange rows don't store the
  tool list that was sent to the API; `show` and the fire path rebuild it
  from `call_type` + the *currently active* `available_moves` preset. If
  the preset has been edited since the call ran, the tools list won't
  match what was originally sent. Check `available_moves.py` if a
  reconstructed tool feels off.
- **Model defaults to current settings.** The base exchange row doesn't
  store the model either, so `show` reports `settings.model` as the
  inherited default. The original call may have used a different model.
  When in doubt, set `model` explicitly in the override.

## Invocation

```!
setopt no_glob 2>/dev/null; set -f; uv run python scripts/exchange_forks.py $ARGUMENTS
```

## Cost

Each sample is a fresh API call with no prompt-cache hit (overrides bust
the cache, and forks don't share a cache prefix with the original run).
For long system prompts × many samples × Opus, this adds up fast.

**Budget reality check:** in a normal agent loop, round-N exchanges run
on a hot prompt cache — input cost is near zero. Forks pay full freight
on the entire input every time, including the multi-block assistant
turns from prior rounds. A round-3 web_research exchange that originally
billed ~$0 (cache hit) can fork at ~$0.50 per Opus sample. A rough way
to spot this: in `show` output, count `user_messages` — anything > 1
means you're carrying prior-round content.

Always surface estimated cost before firing if samples > 1 OR if the
exchange has multiple user_messages. The unified blind path makes
estimation simple: `(input_tokens / 1M) * input_rate * n_samples + small_output_term`
gives a usable number.
