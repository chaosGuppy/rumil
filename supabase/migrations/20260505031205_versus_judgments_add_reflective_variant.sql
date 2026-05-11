-- Add 'reflective' to the versus_judgments.variant CHECK constraint.
--
-- Re-timestamped from 20260502035314 so it applies in forward order
-- relative to migrations that landed on main while this branch was open.
--
-- Originally allowed ('blind', 'ws', 'orch'); the new
-- ReflectiveJudgeWorkflow (read → reflect → verdict, versus-specific
-- iteration target on the judging side) writes rows with
-- variant='reflective' and was being rejected at insert time by the
-- existing constraint.
--
-- Drop and re-add to relax. 'ws' kept in the new constraint for
-- historical-row read compat — older rows with variant='ws' would
-- otherwise become unreadable after a constraint tightening.

ALTER TABLE versus_judgments
    DROP CONSTRAINT versus_judgments_variant_check;

ALTER TABLE versus_judgments
    ADD CONSTRAINT versus_judgments_variant_check
    CHECK (variant IN ('blind', 'ws', 'orch', 'reflective'));
