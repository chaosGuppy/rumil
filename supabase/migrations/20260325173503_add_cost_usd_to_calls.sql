ALTER TABLE public.calls ADD COLUMN cost_usd DOUBLE PRECISION;

UPDATE public.calls SET cost_usd = (
  SELECT COALESCE(SUM(
    CASE
      WHEN (e->>'event') = 'llm_exchange' AND (e->'data'->>'cost_usd') IS NOT NULL
        THEN (e->'data'->>'cost_usd')::double precision
      WHEN (e->>'event') = 'llm_exchange' AND (e->>'cost_usd') IS NOT NULL
        THEN (e->>'cost_usd')::double precision
      ELSE 0
    END
  ), 0)
  FROM jsonb_array_elements(trace_json) AS e
)
WHERE trace_json IS NOT NULL AND jsonb_array_length(trace_json) > 0;

UPDATE public.calls SET cost_usd = NULL WHERE cost_usd = 0;
