-- Add 'axon' to the versus_judgments.variant CHECK constraint.
--
-- Mirrors 20260507025326 (which added 'simple_spine'). AxonWorkflow
-- (axon orchestrator: cache-aware mainline + two-step delegate
-- dispatch, produces_artifact=True so no closer) writes rows with
-- variant='axon' from the judging path and was being rejected at
-- insert time by the existing constraint.

ALTER TABLE versus_judgments
    DROP CONSTRAINT versus_judgments_variant_check;

ALTER TABLE versus_judgments
    ADD CONSTRAINT versus_judgments_variant_check
    CHECK (variant IN ('blind', 'ws', 'orch', 'reflective', 'simple_spine', 'axon'));
