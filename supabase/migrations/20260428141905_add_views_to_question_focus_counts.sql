-- Extend the per-question focus counts to track active `view` pages alongside
-- child_questions, considerations, and judgements. A view is counted for a
-- question Q when it has a `view_of` link pointing at Q. Visibility/staging
-- semantics mirror the other count CTEs.

DROP FUNCTION IF EXISTS compute_project_stats(UUID, TEXT);
DROP FUNCTION IF EXISTS compute_project_stats(UUID);

CREATE OR REPLACE FUNCTION compute_project_stats(
    p_project_id UUID,
    p_staged_run_id TEXT DEFAULT NULL
)
RETURNS jsonb
LANGUAGE sql STABLE AS $$
    WITH superseded_by_events AS (
        SELECT target_id
        FROM mutation_events
        WHERE p_staged_run_id IS NOT NULL
          AND run_id = p_staged_run_id
          AND event_type = 'supersede_page'
    ),
    deleted_by_events AS (
        SELECT target_id
        FROM mutation_events
        WHERE p_staged_run_id IS NOT NULL
          AND run_id = p_staged_run_id
          AND event_type = 'delete_link'
    ),
    latest_credence_overrides AS (
        SELECT DISTINCT ON (target_id) target_id,
               (payload->>'value')::int AS value
        FROM mutation_events
        WHERE p_staged_run_id IS NOT NULL
          AND run_id = p_staged_run_id
          AND event_type = 'set_credence'
          AND payload->>'value' IS NOT NULL
        ORDER BY target_id, created_at DESC
    ),
    latest_robustness_overrides AS (
        SELECT DISTINCT ON (target_id) target_id,
               (payload->>'value')::int AS value
        FROM mutation_events
        WHERE p_staged_run_id IS NOT NULL
          AND run_id = p_staged_run_id
          AND event_type = 'set_robustness'
          AND payload->>'value' IS NOT NULL
        ORDER BY target_id, created_at DESC
    ),
    active_pages AS (
        SELECT p.id,
               p.page_type,
               COALESCE(lco.value, p.credence) AS credence,
               COALESCE(lro.value, p.robustness) AS robustness,
               p.headline
        FROM pages p
        LEFT JOIN latest_credence_overrides lco ON lco.target_id = p.id
        LEFT JOIN latest_robustness_overrides lro ON lro.target_id = p.id
        WHERE p.project_id = p_project_id
          AND p.is_superseded = FALSE
          AND (p.staged = FALSE OR p.run_id = p_staged_run_id)
          AND p.id NOT IN (SELECT target_id FROM superseded_by_events)
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
          AND l.id NOT IN (SELECT target_id FROM deleted_by_events)
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
    child_question_counts AS (
        SELECT l.from_page_id AS question_id, COUNT(*)::int AS n
        FROM page_links l
        JOIN pages cq ON cq.id = l.to_page_id
        WHERE l.link_type = 'child_question'
          AND (l.staged = FALSE OR l.run_id = p_staged_run_id)
          AND l.id NOT IN (SELECT target_id FROM deleted_by_events)
          AND (cq.staged = FALSE OR cq.run_id = p_staged_run_id)
          AND cq.is_superseded = FALSE
          AND cq.id NOT IN (SELECT target_id FROM superseded_by_events)
          AND cq.page_type = 'question'
          AND l.from_page_id IN (SELECT id FROM question_pages)
        GROUP BY l.from_page_id
    ),
    consideration_counts AS (
        SELECT l.to_page_id AS question_id, COUNT(*)::int AS n
        FROM page_links l
        JOIN pages src ON src.id = l.from_page_id
        WHERE l.link_type = 'consideration'
          AND (l.staged = FALSE OR l.run_id = p_staged_run_id)
          AND l.id NOT IN (SELECT target_id FROM deleted_by_events)
          AND (src.staged = FALSE OR src.run_id = p_staged_run_id)
          AND src.is_superseded = FALSE
          AND src.id NOT IN (SELECT target_id FROM superseded_by_events)
          AND l.to_page_id IN (SELECT id FROM question_pages)
        GROUP BY l.to_page_id
    ),
    judgement_counts AS (
        SELECT l.to_page_id AS question_id, COUNT(DISTINCT j.id)::int AS n
        FROM page_links l
        JOIN pages j ON j.id = l.from_page_id
        WHERE j.page_type = 'judgement'
          AND (j.staged = FALSE OR j.run_id = p_staged_run_id)
          AND j.is_superseded = FALSE
          AND j.id NOT IN (SELECT target_id FROM superseded_by_events)
          AND (l.staged = FALSE OR l.run_id = p_staged_run_id)
          AND l.id NOT IN (SELECT target_id FROM deleted_by_events)
          AND l.to_page_id IN (SELECT id FROM question_pages)
        GROUP BY l.to_page_id
    ),
    view_counts AS (
        SELECT l.to_page_id AS question_id, COUNT(DISTINCT v.id)::int AS n
        FROM page_links l
        JOIN pages v ON v.id = l.from_page_id
        WHERE l.link_type = 'view_of'
          AND v.page_type = 'view'
          AND (v.staged = FALSE OR v.run_id = p_staged_run_id)
          AND v.is_superseded = FALSE
          AND v.id NOT IN (SELECT target_id FROM superseded_by_events)
          AND (l.staged = FALSE OR l.run_id = p_staged_run_id)
          AND l.id NOT IN (SELECT target_id FROM deleted_by_events)
          AND l.to_page_id IN (SELECT id FROM question_pages)
        GROUP BY l.to_page_id
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
            ) AS total,
            COALESCE(
                (SELECT n FROM child_question_counts cqc WHERE cqc.question_id = qp.id),
                0
            ) AS child_questions,
            COALESCE(
                (SELECT n FROM consideration_counts cc WHERE cc.question_id = qp.id),
                0
            ) AS considerations,
            COALESCE(
                (SELECT n FROM judgement_counts jc WHERE jc.question_id = qp.id),
                0
            ) AS judgements,
            COALESCE(
                (SELECT n FROM view_counts vc WHERE vc.question_id = qp.id),
                0
            ) AS views
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
                    'total', total,
                    'child_questions', child_questions,
                    'considerations', considerations,
                    'judgements', judgements,
                    'views', views
                ) ORDER BY total DESC
             ) FROM calls_per_question),
            '[]'::jsonb
        )
    );
$$;


DROP FUNCTION IF EXISTS compute_question_stats(TEXT, TEXT);
DROP FUNCTION IF EXISTS compute_question_stats(TEXT);

CREATE OR REPLACE FUNCTION compute_question_stats(
    p_question_id TEXT,
    p_staged_run_id TEXT DEFAULT NULL
)
RETURNS jsonb
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE superseded_by_events AS (
        SELECT target_id
        FROM mutation_events
        WHERE p_staged_run_id IS NOT NULL
          AND run_id = p_staged_run_id
          AND event_type = 'supersede_page'
    ),
    deleted_by_events AS (
        SELECT target_id
        FROM mutation_events
        WHERE p_staged_run_id IS NOT NULL
          AND run_id = p_staged_run_id
          AND event_type = 'delete_link'
    ),
    latest_credence_overrides AS (
        SELECT DISTINCT ON (target_id) target_id,
               (payload->>'value')::int AS value
        FROM mutation_events
        WHERE p_staged_run_id IS NOT NULL
          AND run_id = p_staged_run_id
          AND event_type = 'set_credence'
          AND payload->>'value' IS NOT NULL
        ORDER BY target_id, created_at DESC
    ),
    latest_robustness_overrides AS (
        SELECT DISTINCT ON (target_id) target_id,
               (payload->>'value')::int AS value
        FROM mutation_events
        WHERE p_staged_run_id IS NOT NULL
          AND run_id = p_staged_run_id
          AND event_type = 'set_robustness'
          AND payload->>'value' IS NOT NULL
        ORDER BY target_id, created_at DESC
    ),
    hood(page_id, depth) AS (
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
          AND l.id NOT IN (SELECT target_id FROM deleted_by_events)
    ),
    hood_depths AS (
        SELECT page_id, MIN(depth)::int AS depth
        FROM hood
        GROUP BY page_id
    ),
    active_pages AS (
        SELECT p.id,
               p.page_type,
               COALESCE(lco.value, p.credence) AS credence,
               COALESCE(lro.value, p.robustness) AS robustness,
               p.headline,
               hd.depth
        FROM pages p
        JOIN hood_depths hd ON hd.page_id = p.id
        LEFT JOIN latest_credence_overrides lco ON lco.target_id = p.id
        LEFT JOIN latest_robustness_overrides lro ON lro.target_id = p.id
        WHERE p.is_superseded = FALSE
          AND (p.staged = FALSE OR p.run_id = p_staged_run_id)
          AND p.id NOT IN (SELECT target_id FROM superseded_by_events)
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
          AND l.id NOT IN (SELECT target_id FROM deleted_by_events)
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
    child_question_counts AS (
        SELECT l.from_page_id AS question_id, COUNT(*)::int AS n
        FROM page_links l
        JOIN pages cq ON cq.id = l.to_page_id
        WHERE l.link_type = 'child_question'
          AND (l.staged = FALSE OR l.run_id = p_staged_run_id)
          AND l.id NOT IN (SELECT target_id FROM deleted_by_events)
          AND (cq.staged = FALSE OR cq.run_id = p_staged_run_id)
          AND cq.is_superseded = FALSE
          AND cq.id NOT IN (SELECT target_id FROM superseded_by_events)
          AND cq.page_type = 'question'
          AND l.from_page_id IN (SELECT id FROM question_pages)
        GROUP BY l.from_page_id
    ),
    consideration_counts AS (
        SELECT l.to_page_id AS question_id, COUNT(*)::int AS n
        FROM page_links l
        JOIN pages src ON src.id = l.from_page_id
        WHERE l.link_type = 'consideration'
          AND (l.staged = FALSE OR l.run_id = p_staged_run_id)
          AND l.id NOT IN (SELECT target_id FROM deleted_by_events)
          AND (src.staged = FALSE OR src.run_id = p_staged_run_id)
          AND src.is_superseded = FALSE
          AND src.id NOT IN (SELECT target_id FROM superseded_by_events)
          AND l.to_page_id IN (SELECT id FROM question_pages)
        GROUP BY l.to_page_id
    ),
    judgement_counts AS (
        SELECT l.to_page_id AS question_id, COUNT(DISTINCT j.id)::int AS n
        FROM page_links l
        JOIN pages j ON j.id = l.from_page_id
        WHERE j.page_type = 'judgement'
          AND (j.staged = FALSE OR j.run_id = p_staged_run_id)
          AND j.is_superseded = FALSE
          AND j.id NOT IN (SELECT target_id FROM superseded_by_events)
          AND (l.staged = FALSE OR l.run_id = p_staged_run_id)
          AND l.id NOT IN (SELECT target_id FROM deleted_by_events)
          AND l.to_page_id IN (SELECT id FROM question_pages)
        GROUP BY l.to_page_id
    ),
    view_counts AS (
        SELECT l.to_page_id AS question_id, COUNT(DISTINCT v.id)::int AS n
        FROM page_links l
        JOIN pages v ON v.id = l.from_page_id
        WHERE l.link_type = 'view_of'
          AND v.page_type = 'view'
          AND (v.staged = FALSE OR v.run_id = p_staged_run_id)
          AND v.is_superseded = FALSE
          AND v.id NOT IN (SELECT target_id FROM superseded_by_events)
          AND (l.staged = FALSE OR l.run_id = p_staged_run_id)
          AND l.id NOT IN (SELECT target_id FROM deleted_by_events)
          AND l.to_page_id IN (SELECT id FROM question_pages)
        GROUP BY l.to_page_id
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
            ) AS total,
            COALESCE(
                (SELECT n FROM child_question_counts cqc WHERE cqc.question_id = qp.id),
                0
            ) AS child_questions,
            COALESCE(
                (SELECT n FROM consideration_counts cc WHERE cc.question_id = qp.id),
                0
            ) AS considerations,
            COALESCE(
                (SELECT n FROM judgement_counts jc WHERE jc.question_id = qp.id),
                0
            ) AS judgements,
            COALESCE(
                (SELECT n FROM view_counts vc WHERE vc.question_id = qp.id),
                0
            ) AS views
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
                    'total', total,
                    'child_questions', child_questions,
                    'considerations', considerations,
                    'judgements', judgements,
                    'views', views
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
