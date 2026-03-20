-- Fix race condition in consume_budget: add FOR UPDATE to prevent
-- two concurrent callers from reading the same `used` value.
CREATE OR REPLACE FUNCTION consume_budget(rid TEXT, amount INTEGER)
RETURNS BOOLEAN
LANGUAGE plpgsql AS $$
DECLARE
    cur_total INTEGER;
    cur_used INTEGER;
BEGIN
    SELECT total, used INTO cur_total, cur_used
    FROM budget WHERE run_id = rid FOR UPDATE;
    IF cur_total IS NULL OR (cur_used + amount) > cur_total THEN
        RETURN FALSE;
    END IF;
    UPDATE budget SET used = used + amount WHERE run_id = rid;
    RETURN TRUE;
END;
$$;
