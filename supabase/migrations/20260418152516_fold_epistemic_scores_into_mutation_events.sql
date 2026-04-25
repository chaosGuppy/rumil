-- Retire epistemic_scores. Every non-null credence/robustness score
-- becomes a mutation event, preserving run_id, source_page_id, and
-- created_at so MutationState replay sees the same history.

ALTER TABLE mutation_events
    DROP CONSTRAINT IF EXISTS mutation_events_event_type_check;

ALTER TABLE mutation_events
    ADD CONSTRAINT mutation_events_event_type_check
    CHECK (event_type IN (
        'supersede_page',
        'delete_link',
        'change_link_role',
        'update_page_content',
        'set_credence',
        'set_robustness'
    ));

INSERT INTO mutation_events (id, run_id, event_type, target_id, payload, created_at)
SELECT
  gen_random_uuid(),
  run_id,
  'set_credence',
  page_id,
  jsonb_build_object(
    'value', credence,
    'reasoning', COALESCE(reasoning, ''),
    'source_page_id', source_page_id
  ),
  created_at
FROM epistemic_scores
WHERE credence IS NOT NULL;

INSERT INTO mutation_events (id, run_id, event_type, target_id, payload, created_at)
SELECT
  gen_random_uuid(),
  run_id,
  'set_robustness',
  page_id,
  jsonb_build_object(
    'value', robustness,
    'reasoning', COALESCE(reasoning, ''),
    'source_page_id', source_page_id
  ),
  created_at
FROM epistemic_scores
WHERE robustness IS NOT NULL;

DROP TABLE epistemic_scores;
