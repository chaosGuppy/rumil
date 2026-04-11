---
name: rumil-ingest
description: Commit a source to the workspace and run extraction calls against a question. Creates a Source page (from a file/URL) or reuses an existing one, then runs ingest rounds that turn the source into considerations on the target question. This is the mutating counterpart to rumil-read — use it when the user is ready to spend budget extracting considerations from a source. Tagged origin=claude-code in the run's config. If the user only wants to look at a source or stash it without extraction, reach for rumil-read or rumil-read --save instead.
allowed-tools: Bash
argument-hint: "<file_or_url> --for <question_id> [--budget N] [--smoke-test] | --from-page <page_id> --for <question_id>"
---

# rumil-ingest

Turn a source (file, URL, or previously-saved Source page) into
considerations on a target question. `--for` is **required** — if you
don't have a question to extract against, you want `rumil-read --save`
instead.

This skill creates the Source page (when given a fresh file/URL) and
then runs `ingest_until_done` against the target question. Each round
is a standard INGEST call with the usual context-building and prompts;
Claude Code is just the trigger. Run rows are tagged
`origin=claude-code, skill=rumil-ingest`.

## Two input forms

| Form | Invocation | What happens |
|---|---|---|
| Fresh source | `rumil-ingest <file_or_url> --for <q>` | Fetch → create Source page → run ingest rounds |
| Reuse existing | `rumil-ingest --from-page <src_id> --for <q>` | Skip fetch → run ingest rounds against the existing Source page |

The **reuse** form is the clean composition path with `rumil-read --save`:

```
/rumil-read paper.pdf --save           → creates source 1a2b3c4d
/rumil-ingest --from-page 1a2b3c4d --for abc12345
/rumil-ingest --from-page 1a2b3c4d --for def67890   # same source, different q
```

This is something rumil's CLI doesn't elegantly support today — `main.py
--ingest` always re-creates the source page, so ingesting the same
document against two questions creates two Source pages.

## Defaults

- **Budget**: `--budget 1` by default. `ingest_until_done` typically needs
  one round for short sources and may consume more for long ones — it
  stops when remaining_fruit drops below threshold. Bump the budget if
  you see it bail with "budget exhausted."
- **Workspace**: reads active workspace from `.claude/state/rumil-session.json`.
  Override with `--workspace <name>`.
- **Visibility**: trace URL is printed immediately. Surface it to the
  user in your reply so they can open it alongside the CC session.

## When to use which skill

| Intent | Skill |
|---|---|
| "let me see what's in this PDF" | `rumil-read paper.pdf` |
| "stash this for later, no extraction yet" | `rumil-read paper.pdf --save` |
| "extract considerations from this for question X" | `rumil-ingest paper.pdf --for X` |
| "I already saved this source, now ingest it for Y" | `rumil-ingest --from-page <id> --for Y` |
| "fire one find_considerations / assess / scout on a question" | `rumil-dispatch` |

Ingest is not free — talk to the user first if you're unsure. A useful
rule of thumb: if the user hasn't named both a source **and** a target
question, they probably want `rumil-read`, not this.

## Invocation

```!
PYTHONPATH=.claude/lib uv run python -m rumil_skills.ingest_source $ARGUMENTS
```

## After it runs

The script prints:
- `workspace: …` and `question: <short_id>  <headline>`
- trace URL (surface this)
- `• creating source from file: …` or `• reusing source <id>`
- `→ extracting considerations from <src_id> (budget N)` — start event
- `✓ done: <N> rounds status=… cost=$… budget=used/total` — completion
- The latest INGEST call's `result_summary` if non-empty

If the run fails or extraction output looks off, suggest
`/rumil-trace <call_id>` — grab the call ID from the trace URL.
