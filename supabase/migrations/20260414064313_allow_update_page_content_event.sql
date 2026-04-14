-- Allow 'update_page_content' in mutation_events.event_type.
--
-- The update_page_content event is produced by DB.update_page_content() (e.g.
-- when the create_view call writes a natural-language summary onto an existing
-- View page after its items are created). The writer, the staged-runs replay
-- path (_apply_page_events -> page_content_overrides), and the MutationState
-- cache all already handle it. The CHECK constraint from the original
-- staged-runs migration (20260328102907) didn't know about it though, so every
-- non-staged call that updates a page's content crashes at insert time.
--
-- Postgres doesn't allow altering a CHECK constraint in place, so we DROP +
-- ADD. We also extend DB.stage_run() and DB.commit_staged_run() in Python to
-- handle the new event type (see src/rumil/database.py) — without that, a
-- retroactively staged or committed run would silently fail to revert/apply
-- its content updates.

ALTER TABLE mutation_events
    DROP CONSTRAINT mutation_events_event_type_check;

ALTER TABLE mutation_events
    ADD CONSTRAINT mutation_events_event_type_check
    CHECK (event_type IN (
        'supersede_page',
        'delete_link',
        'change_link_role',
        'update_page_content'
    ));
