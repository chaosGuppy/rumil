---
name: rumil-read
description: Fetch a file or URL into the conversation — the view-only lane for sources. Default mode is pure fetch-and-print with no DB and no LLM, using the same scraping/extraction pipeline that rumil-ingest would use so you can preview exactly what the ingest step would see. Flags layer more behavior — --summary for an LLM headline, --save to persist as a Source page in the workspace. Use whenever the user shares a URL, PDF, or file they want Claude to look at, especially when it's not yet clear whether the source should become a workspace artifact.
allowed-tools: Bash
argument-hint: "<file_or_url> [--save] [--summary] [--full] [--workspace name]"
---

# rumil-read

Load a source — file, URL, or PDF — into the conversation. This is the
**view-only lane**: the default mode opens no DB connection, makes no LLM
call, and does not mutate the workspace. It's for "let me look at this."

Uses rumil's own scraper and file readers (`rumil.scraper.scrape_url`,
`rumil.sources.read_file_content`), so what you see here is byte-identical
to what `rumil-ingest` would feed the extraction step. Preview here,
commit there.

## Modes

- **default** — fetch content, print it. No DB, no LLM.
- **`--summary`** — also generate the 2-3 sentence LLM headline that a
  persisted Source page would get. Uses the LLM but still no DB.
- **`--save`** — persist as a `PageType.SOURCE` page in the active
  workspace. Summarizes automatically (headline comes from the summary).
  **This is the only mode that writes to the workspace.** Still free of
  extraction calls — no considerations are produced.

`--save` implies a summary (create_source_page always generates one), so
`--summary --save` is just `--save`.

## When to use which

| Intent | Command |
|---|---|
| "let me see what's in this PDF" | `rumil-read paper.pdf` |
| "show me this URL and preview the summary" | `rumil-read https://... --summary` |
| "save this to the workspace but don't run extraction yet" | `rumil-read paper.pdf --save` |
| "commit this and extract considerations against question X" | `rumil-ingest paper.pdf --for X` |

If the user wants extraction calls fired, reach for `rumil-ingest`, not
this. `rumil-read --save` is the "stash for later" path — no budget spent.

## Content truncation

Default display truncates at 50,000 chars to keep the conversation
surface manageable. Pass `--full` to dump the entire fetched content
(up to `INGEST_MAX_CHARS` = 500,000). URLs are always fetched at the
full 500k; files are read in full. When `--save` is used, the full
content is persisted regardless of display truncation.

## Workspace

All modes resolve the session workspace from `.claude/state/rumil-session.json`
and print it in the header, overridable with `--workspace name`. Default and
`--summary` modes don't touch the DB, so the workspace is label-only there
(it becomes load-bearing once `--save` writes a Source page).

## Invocation

```!
PYTHONPATH=.claude/lib uv run python -m rumil_skills.read_source $ARGUMENTS
```

## After it runs

- **Default / `--summary`:** source metadata (filename or URL + char count),
  optional summary line, then the content (truncated at 50k unless `--full`).
- **`--save`:** the above plus `✓ saved source <short_id>` and the headline.
  Surface the short ID in your reply — the user may want to reference it
  later (e.g. `rumil-ingest <id> --for <q>`).
