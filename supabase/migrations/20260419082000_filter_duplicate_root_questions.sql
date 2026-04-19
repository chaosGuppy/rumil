-- Hide triage-confirmed duplicates from get_root_questions so the parma
-- question-list view and every other consumer stop rendering both sides
-- of a known dupe pair. Baseline get_root_questions (latest shape) came
-- from 20260418032255_add_snapshot_ts_to_rpcs.sql — we redefine here with
-- one additional WHERE clause that checks extra->'triage'->>'is_duplicate'.
--
-- question_triage writes {is_duplicate: true, duplicate_of: <uuid>} into
-- pages.extra.triage when a newly-created root question embeds too close
-- to an existing one. Prior to this migration nothing in the read path
-- consulted that marker, so the UI showed every not-yet-superseded root
-- question including the known-duplicate side.
--
-- Lossless: the original page stays in the table with triage metadata
-- intact. An operator who wants to see hidden dupes can set
-- `include_duplicates => true`. Default is false so callers that don't
-- pass the flag automatically get the filtered list.


DROP FUNCTION IF EXISTS get_root_questions(TEXT, UUID, TEXT, TIMESTAMPTZ);

CREATE OR REPLACE FUNCTION get_root_questions(
    ws TEXT,
    pid UUID DEFAULT NULL,
    p_staged_run_id TEXT DEFAULT NULL,
    p_snapshot_ts TIMESTAMPTZ DEFAULT NULL,
    p_include_duplicates BOOLEAN DEFAULT FALSE
)
RETURNS SETOF pages
LANGUAGE sql STABLE AS $$
    SELECT p.* FROM pages p
    WHERE p.page_type = 'question'
      AND p.workspace = ws
      AND p.is_superseded = FALSE
      AND (pid IS NULL OR p.project_id = pid)
      AND (p.staged = FALSE OR p.run_id = p_staged_run_id)
      AND (p_snapshot_ts IS NULL
           OR p.run_id = p_staged_run_id
           OR p.created_at <= p_snapshot_ts)
      AND (p_include_duplicates
           OR COALESCE(p.extra->'triage'->>'is_duplicate', 'false') != 'true')
      AND p.id NOT IN (
          SELECT to_page_id FROM page_links
          WHERE link_type = 'child_question'
            AND (staged = FALSE OR run_id = p_staged_run_id)
            AND (p_snapshot_ts IS NULL
                 OR run_id = p_staged_run_id
                 OR created_at <= p_snapshot_ts)
      )
    ORDER BY p.created_at DESC;
$$;
