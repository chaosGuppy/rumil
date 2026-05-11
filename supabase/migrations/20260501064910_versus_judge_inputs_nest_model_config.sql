-- Collapse versus_judgments.judge_inputs's loose sampling / thinking /
-- effort keys into a single nested ``model_config`` dict matching
-- :class:`rumil.model_config.ModelConfig`. Done in-place so the schema
-- is uniform across all rows; ``judge_inputs_hash`` (a STORED
-- GENERATED column over the canonicalized blob) recomputes
-- automatically on UPDATE.
--
-- Old shape:
--   judge_inputs.sampling = {temperature, max_tokens}
--   judge_inputs.thinking = {...} | null
--   judge_inputs.effort = "..." | null
-- New shape:
--   judge_inputs.model_config = {temperature, max_tokens, top_p,
--     thinking, effort, max_thinking_tokens, service_tier}
UPDATE versus_judgments
SET judge_inputs = (
    -- Drop the old sampling/thinking/effort keys, add nested model_config.
    (judge_inputs - 'sampling' - 'thinking' - 'effort')
    || jsonb_build_object(
        'model_config',
        jsonb_build_object(
            'temperature', judge_inputs->'sampling'->'temperature',
            'max_tokens', (judge_inputs->'sampling'->>'max_tokens')::int,
            'top_p', null,
            'thinking', COALESCE(judge_inputs->'thinking', 'null'::jsonb),
            'effort', judge_inputs->>'effort',
            'max_thinking_tokens', null,
            'service_tier', null
        )
    )
)
WHERE judge_inputs ? 'sampling';
