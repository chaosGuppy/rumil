---
name: rumil-deep-research
description: Fire a Google Deep Research run and (on resume) save the synthesis as a Source page in the active rumil workspace. Default mode is fire-and-forget — it starts the run in background and returns the interaction id immediately, so you can keep workshopping in chat while it runs. Resume the same id later to poll to completion and create the Source page. Use whenever the user wants deep web research on a topic or question and is fine with a several-minute wait. Pair with /rumil-ingest afterwards if you want the synthesis extracted into considerations on a question.
allowed-tools: Bash
argument-hint: "<prompt> [--for <q_id>] [--max] [--wait] | --resume <id> | --check <id>"
---

# rumil-deep-research

Kick off a Google Deep Research run (`deep-research-preview-04-2026` or
`deep-research-max-preview-04-2026`) and, once it finishes, persist the
synthesis as a `PageType.SOURCE` in the active workspace. The skill is
the **producer** — it does not run any rumil extraction calls. If you
want considerations extracted from the result, chain `/rumil-ingest
--from-page <src_id> --for <q>` after resume succeeds.

## Three invocation modes

| Intent | Invocation |
|---|---|
| Fire now, check back later | `rumil-deep-research "<prompt>" [--for <q>]` |
| Fire and block until done | `rumil-deep-research "<prompt>" --wait` |
| Poll a running/finished run and save the Source | `rumil-deep-research --resume <id>` |
| Peek at status without polling | `rumil-deep-research --check <id>` |

`--for <q_id>` is **just a tag** — it does not run ingest automatically.
It's carried on the state file so `--resume` can propagate it into the
Source page's `extra.for_question` and print the follow-up `rumil-ingest`
command.

## Fire (default, background)

Starts the run with `background=True` and returns immediately after
printing the interaction id. State is written to
`.claude/state/deep-research/<interaction_id>/` so `--resume` can pick
it up later with no extra args. This is the right mode for the
workshop-in-chat flow:

```
/rumil-deep-research "what are the main methodological critiques of the METR time horizon benchmark?" --for abc12345
→ deep research started: v1_…
• tagged for question: abc12345
• state dir: .claude/state/deep-research/v1_…/

Run `/rumil-deep-research --resume v1_…` when ready.
```

## Wait (block until done)

Same as fire, but polls to terminal in the same invocation and creates
the Source page at the end. Ctrl-C **disconnects without cancelling** —
the remote run continues, and you can `--resume` it. Use `--wait` for
short prompts where the several-minute block is fine.

## Resume

Polls the given interaction id every 15s until it reaches a terminal
status, then:

1. Saves `body.md`, `interaction.json`, `annotations.json` (and any
   images) into the state dir.
2. Calls `create_source_page_from_text(body_md, …)` to persist the
   markdown synthesis as a Source page. Headline is LLM-summarised the
   same way other Sources are.
3. Prints the Source short_id and the `rumil-ingest` command to run
   next (if `--for` was recorded).

The state dir survives after resume — annotations + raw interaction
JSON stay on disk for inspection even though only `body.md` is
persisted as a Source.

## Model choice

- **default**: `deep-research-preview-04-2026` — faster, cheaper.
- **`--max`**: `deep-research-max-preview-04-2026` — slower, more
  thorough. Use for harder prompts.

## What the skill does *not* do (v1)

- Does not fan out cited URLs as their own Source pages. Citations
  live in `extra.annotations` on the main Source.
- Does not run ingest automatically. Chain `/rumil-ingest` explicitly.
- Does not re-summarise or post-process the body markdown — the
  `[cite: N]` markers in the output are preserved verbatim.

## Auth

Uses `google.genai.Client()`, which reads `GEMINI_API_KEY` or
`GOOGLE_API_KEY` from the environment. Export one before firing.

## Invocation

```!
setopt no_glob 2>/dev/null; set -f; PYTHONPATH=.claude/lib uv run python -m rumil_skills.deep_research $ARGUMENTS
```

## After it runs (resume)

- `• polling <id> (interval 15s)`
- `• status: …` lines as the run progresses
- `• terminal status: completed`
- `workspace: <name>`
- `Source created: <full_id>`
- `✓ saved source <short_id>  (<full_id>)`
- `headline: <LLM summary>`
- If `--for` was set: a ready-to-copy `/rumil-ingest --from-page … --for …` line

Surface the Source short_id to the user so they can reference it later.
If the run didn't complete (`failed`, `cancelled`, `incomplete`), the
skill prints the path to the raw `interaction.json` and exits non-zero —
no Source page is created.
