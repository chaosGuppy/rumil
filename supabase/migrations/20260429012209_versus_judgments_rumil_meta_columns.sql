-- Add rumil ws/orch metadata columns to versus_judgments.
--
-- These are runtime artifacts (not config inputs) so they live as their own
-- columns rather than inside judge_inputs — putting them in judge_inputs
-- would contaminate judge_inputs_hash and break dedup. trace_url is derivable
-- from frontend base + run_id + rumil_call_id, so we store the inputs and
-- compose the URL at read time in the API layer.

ALTER TABLE versus_judgments
    ADD COLUMN rumil_question_id  TEXT,
    ADD COLUMN rumil_cost_usd     DOUBLE PRECISION;
