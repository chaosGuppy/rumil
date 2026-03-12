ALTER TABLE public.call_llm_exchanges
    ALTER COLUMN round DROP NOT NULL,
    ALTER COLUMN round SET DEFAULT NULL;
