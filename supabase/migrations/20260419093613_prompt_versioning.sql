-- Prompt versioning: content-addressed tracking of every system prompt ever
-- sent. Filesystem prompts/ remains the source of truth for authoring, but
-- each unique (name, sha256(content)) is registered so we can group runs/
-- exchanges by exact prompt version and diff across versions.
--
-- Kinds:
--   'file'             — a single .md file in prompts/ (e.g. preamble.md)
--                        or a runtime template constant (e.g. _CONTINUE_TEMPLATE)
--   'composite'        — the rendered composite built by build_system_prompt
--                        (preamble + body + citations + grounding [+ addendum])
--   'composite_legacy' — backfilled composite whose parts couldn't be
--                        reconstructed from history; content is stored whole
--
-- The date suffix appended by _with_date_suffix is NOT hashed — it drifts
-- daily and would churn the hash space.

CREATE TABLE public.prompt_versions (
    hash TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'composite', 'composite_legacy')),
    source_path TEXT,
    source_git_commit TEXT,
    parts JSONB,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    seen_count BIGINT NOT NULL DEFAULT 1
);

CREATE INDEX idx_prompt_versions_name ON public.prompt_versions(name, first_seen_at DESC);
CREATE INDEX idx_prompt_versions_kind ON public.prompt_versions(kind);

ALTER TABLE public.prompt_versions ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.call_llm_exchanges
    ADD COLUMN composite_prompt_hash TEXT REFERENCES public.prompt_versions(hash),
    ADD COLUMN prompt_part_hashes JSONB;

CREATE INDEX idx_llm_exchanges_composite_prompt
    ON public.call_llm_exchanges(composite_prompt_hash);

ALTER TABLE public.calls
    ADD COLUMN primary_prompt_hash TEXT REFERENCES public.prompt_versions(hash),
    ADD COLUMN primary_prompt_name TEXT;

CREATE INDEX idx_calls_primary_prompt
    ON public.calls(primary_prompt_name, primary_prompt_hash);

-- Idempotent upsert. Inserts the row if new; otherwise bumps last_seen_at
-- and seen_count. Never overwrites content/name/kind — the hash is the
-- stable identifier.
CREATE OR REPLACE FUNCTION upsert_prompt_version(
    p_hash TEXT,
    p_name TEXT,
    p_content TEXT,
    p_kind TEXT,
    p_source_path TEXT DEFAULT NULL,
    p_source_git_commit TEXT DEFAULT NULL,
    p_parts JSONB DEFAULT NULL
)
RETURNS VOID
LANGUAGE plpgsql
SET search_path = public
AS $$
BEGIN
    INSERT INTO prompt_versions (
        hash, name, content, kind,
        source_path, source_git_commit, parts
    )
    VALUES (
        p_hash, p_name, p_content, p_kind,
        p_source_path, p_source_git_commit, p_parts
    )
    ON CONFLICT (hash) DO UPDATE SET
        last_seen_at = now(),
        seen_count = prompt_versions.seen_count + 1;
END;
$$;
