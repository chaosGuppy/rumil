-- Add 'simple_spine' to the versus_judgments.variant CHECK constraint.
--
-- Mirrors 20260505031205 (which added 'reflective'). The new
-- SimpleSpineWorkflow (structured-rounds main agent loop with parallel
-- subroutine spawns) writes rows with variant='simple_spine' from the
-- judging path and was being rejected at insert time by the existing
-- constraint.

ALTER TABLE versus_judgments
    DROP CONSTRAINT versus_judgments_variant_check;

ALTER TABLE versus_judgments
    ADD CONSTRAINT versus_judgments_variant_check
    CHECK (variant IN ('blind', 'ws', 'orch', 'reflective', 'simple_spine'));
