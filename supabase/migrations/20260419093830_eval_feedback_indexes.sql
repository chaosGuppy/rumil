-- Expression indexes on reputation_events.extra for the eval-feedback loop.
--
-- The "evals as feedback signal" pipeline (see marketplace-thread on eval
-- feedback) queries reputation_events by the page / call a given event is
-- *about* (tagged in extra->'subject_page_id' / extra->'subject_call_id'),
-- not by the run that emitted it. Without these partial indexes those
-- queries are full scans across the project's event set.
--
-- Kept as partial indexes on `extra ? 'subject_*'` so they only cover rows
-- that actually carry the key — preference / baseline events without a
-- subject_* anchor don't bloat the index.

CREATE INDEX IF NOT EXISTS idx_rep_subject_call_id
    ON reputation_events ((extra ->> 'subject_call_id'))
    WHERE extra ? 'subject_call_id';

CREATE INDEX IF NOT EXISTS idx_rep_subject_page_id
    ON reputation_events ((extra ->> 'subject_page_id'))
    WHERE extra ? 'subject_page_id';
