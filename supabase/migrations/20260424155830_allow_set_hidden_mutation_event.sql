-- Allow the `set_hidden` mutation event type, emitted by DB.set_page_hidden
-- when the generative-workflow orchestrator flips an artefact's hidden flag
-- at finalization. Staged runs record the flip as a mutation event so other
-- readers keep seeing the baseline value until the run finalizes.

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
        'set_robustness',
        'set_hidden'
    ));
