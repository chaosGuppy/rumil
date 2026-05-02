-- Inverse of qbp_recurse: returns unspent budget from a failed child cycle to
-- the parent pool. Reduces parent.consumed and child.contributed by `amount`,
-- both floored at zero. Caller computes the refund (typically the unspent
-- slice of a child cycle's allocation, i.e. child.contributed - child.consumed).

CREATE OR REPLACE FUNCTION qbp_refund(rid TEXT, parent_qid TEXT, child_qid TEXT, amount INTEGER)
RETURNS VOID
LANGUAGE plpgsql AS $$
BEGIN
    IF amount <= 0 THEN
        RETURN;
    END IF;
    UPDATE question_budget_pool
        SET consumed = GREATEST(consumed - amount, 0),
            updated_at = now()
        WHERE run_id = rid AND question_id = parent_qid;
    UPDATE question_budget_pool
        SET contributed = GREATEST(contributed - amount, 0),
            updated_at = now()
        WHERE run_id = rid AND question_id = child_qid;
END;
$$;
