-- Projects summary RPC for the public landing page.
--
-- Returns one row per project with:
--   - identity + created_at + hidden flag
--   - question_count (active question pages)
--   - claim_count    (active claim pages)
--   - call_count     (all calls, all-time)
--   - last_activity_at (most recent created_at across pages/calls in the project)
--
-- Designed to be N+1 free: a single SQL call produces every project's summary
-- via GROUP BY + LEFT JOIN, so the API handler does not iterate.
--
-- Baseline-only: excludes staged rows and superseded pages. This matches the
-- semantics of the existing compute_project_stats RPC, and the landing page
-- doesn't render staged-run summaries.

CREATE OR REPLACE FUNCTION list_projects_summary(include_hidden BOOLEAN DEFAULT FALSE)
RETURNS TABLE (
    project_id UUID,
    name TEXT,
    created_at TIMESTAMPTZ,
    hidden BOOLEAN,
    question_count INT,
    claim_count INT,
    call_count INT,
    last_activity_at TIMESTAMPTZ
)
LANGUAGE sql STABLE AS $$
    WITH page_agg AS (
        SELECT
            p.project_id,
            COUNT(*) FILTER (WHERE p.page_type = 'question')::int AS question_count,
            COUNT(*) FILTER (WHERE p.page_type = 'claim')::int    AS claim_count,
            MAX(p.created_at) AS last_page_at
        FROM pages p
        WHERE p.staged = FALSE
          AND p.is_superseded = FALSE
        GROUP BY p.project_id
    ),
    call_agg AS (
        SELECT
            c.project_id,
            COUNT(*)::int AS call_count,
            MAX(c.created_at) AS last_call_at
        FROM calls c
        GROUP BY c.project_id
    )
    SELECT
        pr.id AS project_id,
        pr.name,
        pr.created_at,
        COALESCE(pr.hidden, FALSE) AS hidden,
        COALESCE(pa.question_count, 0) AS question_count,
        COALESCE(pa.claim_count, 0) AS claim_count,
        COALESCE(ca.call_count, 0) AS call_count,
        GREATEST(
            COALESCE(pa.last_page_at, pr.created_at),
            COALESCE(ca.last_call_at, pr.created_at)
        ) AS last_activity_at
    FROM projects pr
    LEFT JOIN page_agg pa ON pa.project_id = pr.id
    LEFT JOIN call_agg ca ON ca.project_id = pr.id
    WHERE include_hidden OR COALESCE(pr.hidden, FALSE) = FALSE
    ORDER BY last_activity_at DESC;
$$;
