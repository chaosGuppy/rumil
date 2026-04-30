-- Add a UNIQUE constraint on (base_exchange_id, overrides_hash, sample_index)
-- so concurrent fire_fork callers can't write duplicate sample_index values
-- under the same overrides group. The original migration created only a
-- non-unique index here, which let racing inserts collide silently and
-- broke the fork-panel column grouping that keys on sample_index.

DROP INDEX IF EXISTS public.idx_exchange_forks_group;

ALTER TABLE public.exchange_forks
    ADD CONSTRAINT uniq_exchange_forks_group
    UNIQUE (base_exchange_id, overrides_hash, sample_index);
