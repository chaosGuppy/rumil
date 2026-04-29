-- Promote essay storage from data/essays/<id>.json + .verdict.json to a
-- shared Postgres table. The team-sync motivation: prefix_hash bakes in
-- _content_hash(essay), so a whitespace-stripping diff in your local
-- essay file vs. mine produces incomparable completions silently. With
-- a single source of truth, schema-version bumps and validator re-runs
-- propagate to everyone on the next read.
--
-- One verdict slot per essay (overwritten on re-validate) — matches the
-- prior cache-file behaviour. verdict_request / verdict_response capture
-- the raw provider-shaped API call to the validator so we can audit /
-- replay it under a future model + content-address validator changes.
--
-- raw_html is nullable: the fetcher's byte-cache stays on disk
-- (data/raw_html/), but having a copy in the row means anyone querying
-- the DB from elsewhere can see the original markup without filesystem
-- access. Backfill skips it; future fresh fetches populate.
--
-- content_hash is generated from markdown so the prefix_config_hash that
-- everything else keys on stays in lockstep with the canonical essay
-- body — no drift between "the cached hash" and "the live computation."

CREATE TABLE versus_essays (
    id              TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    author          TEXT NOT NULL,
    pub_date        TEXT NOT NULL,
    blocks          JSONB NOT NULL,
    markdown        TEXT NOT NULL,
    image_count     INT NOT NULL DEFAULT 0,
    schema_version  INT NOT NULL,
    raw_html        TEXT,
    content_hash    TEXT GENERATED ALWAYS AS (
        encode(extensions.digest(markdown, 'sha256'), 'hex')
    ) STORED,
    -- Validator verdict (one slot, overwritten on re-validate).
    verdict_clean      BOOLEAN,
    verdict_issues     JSONB,
    verdict_model      TEXT,
    verdict_version    INT,
    verdict_request    JSONB,
    verdict_response   JSONB,
    verdict_at         TIMESTAMPTZ,
    fetched_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE versus_essays ENABLE ROW LEVEL SECURITY;

CREATE INDEX idx_versus_essays_source ON versus_essays(source_id);
CREATE INDEX idx_versus_essays_content_hash ON versus_essays(content_hash);
