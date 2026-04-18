-- B03 + B08: add staged column to tables that hold research artifacts but
-- missed the staged-runs rollout.
--
-- Tables affected:
--   page_ratings         (B03) — feedback on pages
--   page_flags           (B03) — operator-visible issue/funniness/duplicate markers
--   epistemic_scores     (B08) — per-run credence/robustness overrides
--   call_llm_exchanges   (B08) — LLM I/O audit trail
--   page_format_events   (B08) — fertility reconstruction data
--
-- All five already have a run_id column; we only need to add staged.
-- Existing rows remain baseline (default FALSE).

ALTER TABLE page_ratings ADD COLUMN staged BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE page_flags ADD COLUMN staged BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE epistemic_scores ADD COLUMN staged BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE call_llm_exchanges ADD COLUMN staged BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE page_format_events ADD COLUMN staged BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX idx_page_ratings_staged ON page_ratings(staged) WHERE staged = TRUE;
CREATE INDEX idx_page_flags_staged ON page_flags(staged) WHERE staged = TRUE;
CREATE INDEX idx_epistemic_scores_staged ON epistemic_scores(staged) WHERE staged = TRUE;
CREATE INDEX idx_call_llm_exchanges_staged ON call_llm_exchanges(staged) WHERE staged = TRUE;
CREATE INDEX idx_page_format_events_staged ON page_format_events(staged) WHERE staged = TRUE;
