---
name: rumil-nudge
description: Leave a mid-run steering nudge on an active rumil run — note, constrain, veto, redo, rewrite, pause, resume, list, revoke. The same primitive as parma chat / inline UI / CLI. Use when the user wants to influence an in-flight orchestrator without waiting for it to finish. NL-first — Claude reads the run state, proposes a structured nudge, confirms, then fires.
allowed-tools: Bash
argument-hint: "<run_id> <verb> ... | revoke <nudge_id>"
---

# rumil-nudge

> **Under the hood:** this skill wraps `scripts/nudge.py`, which writes a
> typed row to the `run_nudges` table. The orchestrator consumes active
> nudges between dispatch batches (hard filters) and each call consumes
> them at context-build time (soft injection). See
> `src/rumil/nudges/consumer.py` for the read-side logic.

Leave a steering nudge on a live rumil run. One primitive, many shapes —
the skill's job is to translate what the user said into the right
structured nudge and fire it.

## When to reach for this

- **"Stop doing X / stop going down the rabbit hole on Y"** → `constrain` with `--ban-types` (hard filter on dispatch).
- **"Focus more on ..."** / **"Make sure to consider ..."** → `note` (soft NL context injection).
- **"That call was garbage / ignore its output"** → `veto <call_id>`.
- **"Redo that call with ..."** → `redo <call_id>`, follow up with a `note` carrying the framing.
- **"What we really want to know is ..."** → `rewrite` (overlay-only in v1; does not mutate the root question page).
- **"Pause the run / I need to think"** → `pause` (writes `runs.paused_at`; orchestrator's `wait_while_paused` stops the loop at the next safe boundary).
- **"OK resume"** → `resume`.
- **"What nudges are active?"** → `list`.

If the user's intent could map to multiple kinds (e.g. "don't do web research on this" could be `constrain --ban-types web_research` OR a note), prefer the structured kind — it's enforced, and the user meant it.

## Defaults

- **Durability**: one-shot by default for all kinds except `rewrite` and `pause` (those are inherently persistent). Add `--persistent` to make a note or constraint ambient for the run.
- **Author**: always pass `--author claude` when firing via this skill so the nudge is attributed to Claude-via-skill, not a raw human action. Useful for auditing.
- **Confirm before firing**: if the nudge would ban a call type or pause the run, surface the exact CLI line and confirm with the user before running it. Soft notes can fire without confirmation unless the user's request was ambiguous.

## Invocation

```!
uv run python scripts/nudge.py $ARGUMENTS --author claude
```

(Note: `--author claude` is appended by convention; if the user passes their own `--author` in `$ARGUMENTS`, use that instead and drop the default.)

## After it runs

The script prints a compact line per affected nudge:

```
ae6f762b  inject_note           active    soft,persistent   scope=[-]  focus on the economic angle
```

Surface that line back to the user so they know the nudge landed, and if it's hard/persistent remind them how to revoke (`/rumil-nudge revoke <id>`).

## Listing / revoking

- `<run_id> list` — shows currently-active nudges. `<run_id> list --status all` for the full history including consumed/revoked.
- `revoke <nudge_id>` — flips a nudge's status to `revoked` and stamps `revoked_at`. Once consumed/expired, a nudge can't be revoked (use a fresh nudge if the user wants to change direction).
