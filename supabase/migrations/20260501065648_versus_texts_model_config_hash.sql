-- Add model_config_hash to versus_texts so analytics can distinguish
-- two rows produced under different model conditions. Generated
-- naturally as the canonicalized SHA256 of ``request->'model_config'``,
-- so it forks deterministically when the registry's per-model config
-- changes (sampling, thinking, effort, max_thinking_tokens, etc.).
--
-- Older rows (pre-registry) don't have ``model_config`` in their
-- request blob; they get NULL and form an implicit "legacy" bucket.
-- Aggregations grouping on (source_id, model_config_hash) treat
-- NULL-hash rows as their own variant, which is the right default
-- given the legacy rows were produced under a different code path.

ALTER TABLE versus_texts ADD COLUMN model_config_hash TEXT GENERATED ALWAYS AS (
    CASE
        WHEN request ? 'model_config'
        THEN encode(digest((request->'model_config')::text, 'sha256'), 'hex')
        ELSE NULL
    END
) STORED;

CREATE INDEX idx_versus_texts_model_config_hash
    ON versus_texts (source_id, model_config_hash);
