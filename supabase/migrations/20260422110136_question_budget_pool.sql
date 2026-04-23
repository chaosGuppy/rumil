-- Per-question shared budget pool for prioritisation cycles.
-- Multiple TwoPhase / ClaimInvestigation orchestrators working on the same
-- question contribute their assigned budget to a shared pool and draw from
-- it together. The run-level `budget` table remains the authoritative
-- ceiling; this table is a coordination layer on top.

CREATE TABLE question_budget_pool (
    run_id        TEXT NOT NULL,
    question_id   TEXT NOT NULL,
    contributed   INTEGER NOT NULL DEFAULT 0,
    consumed      INTEGER NOT NULL DEFAULT 0,
    active_calls  INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, question_id)
);

ALTER TABLE question_budget_pool ENABLE ROW LEVEL SECURITY;

CREATE INDEX idx_qbp_run_id ON question_budget_pool(run_id);

CREATE OR REPLACE FUNCTION qbp_register(rid TEXT, qid TEXT, contribution INTEGER)
RETURNS TABLE(contributed INTEGER, consumed INTEGER, active_calls INTEGER)
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO question_budget_pool (run_id, question_id, contributed, active_calls)
    VALUES (rid, qid, contribution, 1)
    ON CONFLICT (run_id, question_id) DO UPDATE
        SET contributed  = question_budget_pool.contributed + EXCLUDED.contributed,
            active_calls = question_budget_pool.active_calls + 1,
            updated_at   = now();
    RETURN QUERY
        SELECT p.contributed, p.consumed, p.active_calls
        FROM question_budget_pool p
        WHERE p.run_id = rid AND p.question_id = qid;
END;
$$;

CREATE OR REPLACE FUNCTION qbp_consume(rid TEXT, qid TEXT, amount INTEGER)
RETURNS TABLE(remaining INTEGER, exhausted BOOLEAN)
LANGUAGE plpgsql AS $$
DECLARE
    cur_c INTEGER;
    cur_u INTEGER;
BEGIN
    SELECT p.contributed, p.consumed INTO cur_c, cur_u
    FROM question_budget_pool p
    WHERE p.run_id = rid AND p.question_id = qid
    FOR UPDATE;
    IF cur_c IS NULL THEN
        -- No pool registered. Return sentinel "never exhausted" so callers
        -- (e.g. legacy / rumil-mediated dispatch lane) don't accidentally
        -- early-exit. The run-level budget remains the authoritative gate.
        RETURN QUERY SELECT 2147483647::INTEGER, FALSE;
        RETURN;
    END IF;
    UPDATE question_budget_pool
        SET consumed = consumed + amount, updated_at = now()
        WHERE run_id = rid AND question_id = qid;
    RETURN QUERY
        SELECT (cur_c - (cur_u + amount))::INTEGER,
               ((cur_u + amount) >= cur_c);
END;
$$;

CREATE OR REPLACE FUNCTION qbp_unregister(rid TEXT, qid TEXT)
RETURNS VOID
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE question_budget_pool
        SET active_calls = GREATEST(active_calls - 1, 0),
            updated_at = now()
        WHERE run_id = rid AND question_id = qid;
END;
$$;

CREATE OR REPLACE FUNCTION qbp_recurse(rid TEXT, parent_qid TEXT, child_qid TEXT, amount INTEGER)
RETURNS VOID
LANGUAGE plpgsql AS $$
BEGIN
    -- Charge the parent pool: the recurse budget is committed against the
    -- parent cycle. Then register the child contribution. Atomic so peer
    -- cycles never observe momentarily-doubled budget.
    PERFORM qbp_consume(rid, parent_qid, amount);
    PERFORM qbp_register(rid, child_qid, amount);
END;
$$;
