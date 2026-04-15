-- Widen mutation_events.event_type CHECK to include 'update_page_content'.
--
-- The staged-runs migration (20260328102907) added a CHECK constraint that
-- only permitted 'supersede_page', 'delete_link', 'change_link_role'. Later,
-- DB.update_page_content was added to src/rumil/database.py and records an
-- 'update_page_content' event, but the CHECK was never widened. Every call
-- to update_page_content (e.g. from the create_view closing review) failed
-- with a Postgres CHECK-violation error (chaosGuppy/rumil#281).
--
-- The Python replay side (_load_mutation_state, _apply_page_events) already
-- handles 'update_page_content' correctly; only the DB constraint is missing.

ALTER TABLE mutation_events
    DROP CONSTRAINT IF EXISTS mutation_events_event_type_check;

ALTER TABLE mutation_events
    ADD CONSTRAINT mutation_events_event_type_check
    CHECK (event_type IN (
        'supersede_page',
        'delete_link',
        'change_link_role',
        'update_page_content'
    ));
