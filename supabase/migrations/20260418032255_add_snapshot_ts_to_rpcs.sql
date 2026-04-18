-- Fork-at-snapshot: add p_snapshot_ts TIMESTAMPTZ DEFAULT NULL to every RPC
-- that reads pages or page_links, so staged runs can be pinned to a fixed
-- baseline view.
--
-- Semantics (per marketplace-thread/11-staging-concurrency.md §4):
-- When p_snapshot_ts IS NOT NULL, a row is visible iff:
--   created_at <= p_snapshot_ts  OR  run_id = p_staged_run_id
-- Non-staged callers pass NULL for both params and everything behaves as before.

-- Utility: expose server now() so staged-run snapshot boundaries can be
-- pinned to the database clock rather than the client clock.
CREATE OR REPLACE FUNCTION db_now()
RETURNS TIMESTAMPTZ
LANGUAGE sql STABLE AS $$
    SELECT now();
$$;

DROP FUNCTION IF EXISTS match_pages(
    extensions.vector, DOUBLE PRECISION, INTEGER, TEXT, UUID, TEXT, TEXT
);

CREATE OR REPLACE FUNCTION match_pages(
    query_embedding extensions.vector(1024),
    match_threshold DOUBLE PRECISION DEFAULT 0.5,
    match_count INTEGER DEFAULT 10,
    filter_workspace TEXT DEFAULT NULL,
    filter_project_id UUID DEFAULT NULL,
    filter_field_name TEXT DEFAULT NULL,
    filter_staged_run_id TEXT DEFAULT NULL,
    p_snapshot_ts TIMESTAMPTZ DEFAULT NULL
)
RETURNS TABLE(
    id TEXT,
    page_type TEXT,
    layer TEXT,
    workspace TEXT,
    content TEXT,
    headline TEXT,
    abstract TEXT,
    project_id UUID,
    epistemic_status DOUBLE PRECISION,
    epistemic_type TEXT,
    credence INTEGER,
    robustness INTEGER,
    provenance_model TEXT,
    provenance_call_type TEXT,
    provenance_call_id TEXT,
    created_at TIMESTAMPTZ,
    superseded_by TEXT,
    is_superseded BOOLEAN,
    extra JSONB,
    run_id TEXT,
    field_name TEXT,
    similarity DOUBLE PRECISION
)
LANGUAGE sql STABLE
SET search_path = public, extensions
AS $$
    SELECT
        p.id,
        p.page_type,
        p.layer,
        p.workspace,
        p.content,
        p.headline,
        p.abstract,
        p.project_id,
        p.epistemic_status,
        p.epistemic_type,
        p.credence,
        p.robustness,
        p.provenance_model,
        p.provenance_call_type,
        p.provenance_call_id,
        p.created_at,
        p.superseded_by,
        p.is_superseded,
        p.extra,
        p.run_id,
        pe.field_name,
        1 - (pe.embedding <=> query_embedding) AS similarity
    FROM page_embeddings pe
    JOIN pages p ON p.id = pe.page_id
    WHERE p.is_superseded = FALSE
      AND 1 - (pe.embedding <=> query_embedding) > match_threshold
      AND (filter_workspace IS NULL OR p.workspace = filter_workspace)
      AND (filter_project_id IS NULL OR p.project_id = filter_project_id)
      AND (filter_field_name IS NULL OR pe.field_name = filter_field_name)
      AND (p.staged = FALSE OR p.run_id = filter_staged_run_id)
      AND (p_snapshot_ts IS NULL
           OR p.run_id = filter_staged_run_id
           OR p.created_at <= p_snapshot_ts)
    ORDER BY pe.embedding <=> query_embedding
    LIMIT match_count;
$$;


DROP FUNCTION IF EXISTS get_root_questions(TEXT, UUID, TEXT);

CREATE OR REPLACE FUNCTION get_root_questions(
    ws TEXT,
    pid UUID DEFAULT NULL,
    p_staged_run_id TEXT DEFAULT NULL,
    p_snapshot_ts TIMESTAMPTZ DEFAULT NULL
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


DROP FUNCTION IF EXISTS compute_project_stats(UUID, TEXT);

CREATE OR REPLACE FUNCTION compute_project_stats(
    p_project_id UUID,
    p_staged_run_id TEXT DEFAULT NULL,
    p_snapshot_ts TIMESTAMPTZ DEFAULT NULL
)
RETURNS jsonb
LANGUAGE sql STABLE AS $$
    WITH active_pages AS (
        SELECT id, page_type, credence, robustness, headline
        FROM pages
        WHERE project_id = p_project_id
          AND is_superseded = FALSE
          AND (staged = FALSE OR run_id = p_staged_run_id)
          AND (p_snapshot_ts IS NULL
               OR run_id = p_staged_run_id
               OR created_at <= p_snapshot_ts)
    ),
    active_links AS (
        SELECT l.id,
               pf.page_type AS from_type,
               pt.page_type AS to_type,
               l.link_type
        FROM page_links l
        JOIN active_pages pf ON pf.id = l.from_page_id
        JOIN active_pages pt ON pt.id = l.to_page_id
        WHERE (l.staged = FALSE OR l.run_id = p_staged_run_id)
          AND (p_snapshot_ts IS NULL
               OR l.run_id = p_staged_run_id
               OR l.created_at <= p_snapshot_ts)
    ),
    pages_by_type AS (
        SELECT page_type, COUNT(*)::int AS n
        FROM active_pages
        GROUP BY page_type
    ),
    links_by_type AS (
        SELECT link_type, COUNT(*)::int AS n
        FROM active_links
        GROUP BY link_type
    ),
    out_counts AS (
        SELECT from_type AS page_type, link_type, COUNT(*)::float AS n
        FROM active_links
        GROUP BY from_type, link_type
    ),
    in_counts AS (
        SELECT to_type AS page_type, link_type, COUNT(*)::float AS n
        FROM active_links
        GROUP BY to_type, link_type
    ),
    degree_pairs AS (
        SELECT page_type, link_type FROM out_counts
        UNION
        SELECT page_type, link_type FROM in_counts
    ),
    degree_cells AS (
        SELECT
            dp.page_type,
            dp.link_type,
            COALESCE(oc.n, 0) / pbt.n::float AS avg_out,
            COALESCE(ic.n, 0) / pbt.n::float AS avg_in
        FROM degree_pairs dp
        JOIN pages_by_type pbt ON pbt.page_type = dp.page_type
        LEFT JOIN out_counts oc
               ON oc.page_type = dp.page_type AND oc.link_type = dp.link_type
        LEFT JOIN in_counts ic
               ON ic.page_type = dp.page_type AND ic.link_type = dp.link_type
    ),
    robustness_hist AS (
        SELECT robustness AS bucket, COUNT(*)::int AS n
        FROM active_pages
        WHERE robustness IS NOT NULL
        GROUP BY robustness
    ),
    credence_hist AS (
        SELECT credence AS bucket, COUNT(*)::int AS n
        FROM active_pages
        WHERE credence IS NOT NULL
        GROUP BY credence
    ),
    question_pages AS (
        SELECT id, headline
        FROM active_pages
        WHERE page_type = 'question'
    ),
    calls_by_qt AS (
        SELECT c.scope_page_id AS question_id,
               c.call_type,
               COUNT(*)::int AS n
        FROM calls c
        WHERE c.project_id = p_project_id
          AND c.scope_page_id IN (SELECT id FROM question_pages)
        GROUP BY c.scope_page_id, c.call_type
    ),
    calls_per_question AS (
        SELECT
            qp.id AS question_id,
            qp.headline,
            COALESCE(
                (SELECT jsonb_object_agg(cbt.call_type, cbt.n)
                 FROM calls_by_qt cbt WHERE cbt.question_id = qp.id),
                '{}'::jsonb
            ) AS by_type,
            COALESCE(
                (SELECT SUM(cbt.n)::int FROM calls_by_qt cbt WHERE cbt.question_id = qp.id),
                0
            ) AS total
        FROM question_pages qp
    )
    SELECT jsonb_build_object(
        'pages_total', COALESCE((SELECT SUM(n)::int FROM pages_by_type), 0),
        'pages_by_type', COALESCE(
            (SELECT jsonb_object_agg(page_type, n) FROM pages_by_type),
            '{}'::jsonb
        ),
        'links_total', COALESCE((SELECT SUM(n)::int FROM links_by_type), 0),
        'links_by_type', COALESCE(
            (SELECT jsonb_object_agg(link_type, n) FROM links_by_type),
            '{}'::jsonb
        ),
        'degree_matrix', COALESCE(
            (SELECT jsonb_object_agg(page_type, per_link)
             FROM (
                 SELECT page_type,
                        jsonb_object_agg(
                            link_type,
                            jsonb_build_object('avg_out', avg_out, 'avg_in', avg_in)
                        ) AS per_link
                 FROM degree_cells
                 GROUP BY page_type
             ) _grouped),
            '{}'::jsonb
        ),
        'robustness_histogram', COALESCE(
            (SELECT jsonb_object_agg(bucket::text, n) FROM robustness_hist),
            '{}'::jsonb
        ),
        'credence_histogram', COALESCE(
            (SELECT jsonb_object_agg(bucket::text, n) FROM credence_hist),
            '{}'::jsonb
        ),
        'calls_per_question', COALESCE(
            (SELECT jsonb_agg(
                jsonb_build_object(
                    'question_id', question_id,
                    'headline', headline,
                    'by_type', by_type,
                    'total', total
                ) ORDER BY total DESC
             ) FROM calls_per_question),
            '[]'::jsonb
        )
    );
$$;


DROP FUNCTION IF EXISTS compute_question_stats(TEXT, TEXT);

CREATE OR REPLACE FUNCTION compute_question_stats(
    p_question_id TEXT,
    p_staged_run_id TEXT DEFAULT NULL,
    p_snapshot_ts TIMESTAMPTZ DEFAULT NULL
)
RETURNS jsonb
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE hood(page_id, depth) AS (
        SELECT p_question_id, 0
        UNION
        SELECT
            CASE WHEN l.from_page_id = h.page_id
                 THEN l.to_page_id
                 ELSE l.from_page_id END,
            h.depth + 1
        FROM hood h
        JOIN page_links l
          ON (l.from_page_id = h.page_id OR l.to_page_id = h.page_id)
        WHERE h.depth < 2
          AND (l.staged = FALSE OR l.run_id = p_staged_run_id)
          AND (p_snapshot_ts IS NULL
               OR l.run_id = p_staged_run_id
               OR l.created_at <= p_snapshot_ts)
    ),
    hood_depths AS (
        SELECT page_id, MIN(depth)::int AS depth
        FROM hood
        GROUP BY page_id
    ),
    active_pages AS (
        SELECT p.id, p.page_type, p.credence, p.robustness, p.headline,
               hd.depth
        FROM pages p
        JOIN hood_depths hd ON hd.page_id = p.id
        WHERE p.is_superseded = FALSE
          AND (p.staged = FALSE OR p.run_id = p_staged_run_id)
          AND (p_snapshot_ts IS NULL
               OR p.run_id = p_staged_run_id
               OR p.created_at <= p_snapshot_ts)
    ),
    active_links AS (
        SELECT l.id,
               l.from_page_id,
               l.to_page_id,
               pf.page_type AS from_type,
               pt.page_type AS to_type,
               l.link_type
        FROM page_links l
        JOIN active_pages pf ON pf.id = l.from_page_id
        JOIN active_pages pt ON pt.id = l.to_page_id
        WHERE (l.staged = FALSE OR l.run_id = p_staged_run_id)
          AND (p_snapshot_ts IS NULL
               OR l.run_id = p_staged_run_id
               OR l.created_at <= p_snapshot_ts)
    ),
    pages_by_type AS (
        SELECT page_type, COUNT(*)::int AS n
        FROM active_pages
        GROUP BY page_type
    ),
    links_by_type AS (
        SELECT link_type, COUNT(*)::int AS n
        FROM active_links
        GROUP BY link_type
    ),
    out_counts AS (
        SELECT from_type AS page_type, link_type, COUNT(*)::float AS n
        FROM active_links
        GROUP BY from_type, link_type
    ),
    in_counts AS (
        SELECT to_type AS page_type, link_type, COUNT(*)::float AS n
        FROM active_links
        GROUP BY to_type, link_type
    ),
    degree_pairs AS (
        SELECT page_type, link_type FROM out_counts
        UNION
        SELECT page_type, link_type FROM in_counts
    ),
    degree_cells AS (
        SELECT
            dp.page_type,
            dp.link_type,
            COALESCE(oc.n, 0) / pbt.n::float AS avg_out,
            COALESCE(ic.n, 0) / pbt.n::float AS avg_in
        FROM degree_pairs dp
        JOIN pages_by_type pbt ON pbt.page_type = dp.page_type
        LEFT JOIN out_counts oc
               ON oc.page_type = dp.page_type AND oc.link_type = dp.link_type
        LEFT JOIN in_counts ic
               ON ic.page_type = dp.page_type AND ic.link_type = dp.link_type
    ),
    robustness_hist AS (
        SELECT robustness AS bucket, COUNT(*)::int AS n
        FROM active_pages
        WHERE robustness IS NOT NULL
        GROUP BY robustness
    ),
    credence_hist AS (
        SELECT credence AS bucket, COUNT(*)::int AS n
        FROM active_pages
        WHERE credence IS NOT NULL
        GROUP BY credence
    ),
    question_pages AS (
        SELECT id, headline
        FROM active_pages
        WHERE page_type = 'question'
    ),
    calls_by_qt AS (
        SELECT c.scope_page_id AS question_id,
               c.call_type,
               COUNT(*)::int AS n
        FROM calls c
        WHERE c.scope_page_id IN (SELECT id FROM question_pages)
        GROUP BY c.scope_page_id, c.call_type
    ),
    calls_per_question AS (
        SELECT
            qp.id AS question_id,
            qp.headline,
            COALESCE(
                (SELECT jsonb_object_agg(cbt.call_type, cbt.n)
                 FROM calls_by_qt cbt WHERE cbt.question_id = qp.id),
                '{}'::jsonb
            ) AS by_type,
            COALESCE(
                (SELECT SUM(cbt.n)::int FROM calls_by_qt cbt WHERE cbt.question_id = qp.id),
                0
            ) AS total
        FROM question_pages qp
    )
    SELECT jsonb_build_object(
        'subgraph_page_count', (SELECT COUNT(*)::int FROM active_pages),
        'pages_total', COALESCE((SELECT SUM(n)::int FROM pages_by_type), 0),
        'pages_by_type', COALESCE(
            (SELECT jsonb_object_agg(page_type, n) FROM pages_by_type),
            '{}'::jsonb
        ),
        'links_total', COALESCE((SELECT SUM(n)::int FROM links_by_type), 0),
        'links_by_type', COALESCE(
            (SELECT jsonb_object_agg(link_type, n) FROM links_by_type),
            '{}'::jsonb
        ),
        'degree_matrix', COALESCE(
            (SELECT jsonb_object_agg(page_type, per_link)
             FROM (
                 SELECT page_type,
                        jsonb_object_agg(
                            link_type,
                            jsonb_build_object('avg_out', avg_out, 'avg_in', avg_in)
                        ) AS per_link
                 FROM degree_cells
                 GROUP BY page_type
             ) _grouped),
            '{}'::jsonb
        ),
        'robustness_histogram', COALESCE(
            (SELECT jsonb_object_agg(bucket::text, n) FROM robustness_hist),
            '{}'::jsonb
        ),
        'credence_histogram', COALESCE(
            (SELECT jsonb_object_agg(bucket::text, n) FROM credence_hist),
            '{}'::jsonb
        ),
        'calls_per_question', COALESCE(
            (SELECT jsonb_agg(
                jsonb_build_object(
                    'question_id', question_id,
                    'headline', headline,
                    'by_type', by_type,
                    'total', total
                ) ORDER BY total DESC
             ) FROM calls_per_question),
            '[]'::jsonb
        ),
        'subgraph', jsonb_build_object(
            'nodes', COALESCE(
                (SELECT jsonb_agg(
                    jsonb_build_object(
                        'id', id,
                        'page_type', page_type,
                        'headline', headline,
                        'depth', depth
                    ) ORDER BY depth, id
                 ) FROM active_pages),
                '[]'::jsonb
            ),
            'edges', COALESCE(
                (SELECT jsonb_agg(
                    jsonb_build_object(
                        'from_page_id', from_page_id,
                        'to_page_id', to_page_id,
                        'link_type', link_type
                    ) ORDER BY from_page_id, to_page_id
                 ) FROM active_links),
                '[]'::jsonb
            )
        )
    );
$$;
