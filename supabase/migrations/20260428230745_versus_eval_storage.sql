-- Versus eval storage. Two tables replace the JSONL files in versus/data/.
--
-- versus_texts: any essay-shaped text (human baseline, model continuation,
-- model paraphrase) plus the conditions that produced it. The raw provider
-- request/response (where applicable) is the source of truth; request_hash
-- is a generated content-address used for "have we run this exact config?"
-- lookups. No DB-level uniqueness — re-running at the same config (e.g.
-- temp>0 sampling) appends new rows; "skip if exists" is a runner-side
-- query, not an enforced constraint.
--
-- versus_judgments: one pairwise verdict on a (text_a, text_b) pair under
-- a criterion + judge config. judge_inputs is the canonical condition blob
-- (prompt content, sampling, tools, pair surface, workspace state, code
-- fingerprint, budget, closer config) and naturally folds in references
-- to the specific texts judged so re-judging different completion samples
-- forks the hash. project_id, run_id, and rumil_call_id are soft references
-- (no FK) populated only when the judgment came from a rumil orchestrator/ws
-- call — soft because the rumil run may have been pruned or live in another DB.
--
-- request/response are stored as raw provider-shaped JSONB. For orch
-- judgments there's no single API call, so request/response are null and
-- the agent transcript lives in rumil's calls.trace_json (joined via
-- rumil_call_id). RLS is enabled with no policies; service_role bypasses.

CREATE TABLE versus_texts (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    essay_id      TEXT NOT NULL,
    -- 'paraphrase' is intentionally absent for now. Paraphrase generation +
    -- the slicing pipeline are deferred; if/when they come back, ALTER the
    -- CHECK to add the new value(s).
    kind          TEXT NOT NULL CHECK (kind IN ('human', 'completion')),
    source_id     TEXT NOT NULL,
    prefix_hash   TEXT,
    model_id      TEXT,
    request       JSONB,
    response      JSONB,
    text          TEXT NOT NULL,
    params        JSONB NOT NULL DEFAULT '{}'::jsonb,
    request_hash  TEXT GENERATED ALWAYS AS (
        CASE WHEN request IS NULL THEN NULL
             ELSE encode(extensions.digest(request::text, 'sha256'), 'hex')
        END
    ) STORED,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE versus_texts ENABLE ROW LEVEL SECURITY;

CREATE INDEX idx_versus_texts_essay_kind_source ON versus_texts(essay_id, kind, source_id);
CREATE INDEX idx_versus_texts_lookup ON versus_texts(essay_id, kind, source_id, prefix_hash, request_hash);
CREATE INDEX idx_versus_texts_model ON versus_texts(model_id) WHERE model_id IS NOT NULL;

CREATE TABLE versus_judgments (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    essay_id            TEXT NOT NULL,
    prefix_hash         TEXT NOT NULL,
    source_a            TEXT NOT NULL,
    source_b            TEXT NOT NULL,
    display_first       TEXT NOT NULL,
    text_a_id           UUID NOT NULL REFERENCES versus_texts(id) ON DELETE RESTRICT,
    text_b_id           UUID NOT NULL REFERENCES versus_texts(id) ON DELETE RESTRICT,
    criterion           TEXT NOT NULL,
    variant             TEXT NOT NULL CHECK (variant IN ('blind', 'ws', 'orch')),
    judge_model         TEXT NOT NULL,
    request             JSONB,
    response            JSONB,
    judge_inputs        JSONB NOT NULL,
    judge_inputs_hash   TEXT GENERATED ALWAYS AS (
        encode(extensions.digest(judge_inputs::text, 'sha256'), 'hex')
    ) STORED,
    -- verdict is nullable: refusals and parse failures legitimately produce a
    -- judgment row with reasoning_text but no extractable A/B/tie verdict.
    verdict             TEXT CHECK (verdict IS NULL OR verdict IN ('A', 'B', 'tie')),
    preference_label    TEXT,
    reasoning_text      TEXT NOT NULL,
    duration_s          DOUBLE PRECISION,
    project_id          UUID,
    run_id              TEXT,
    rumil_call_id       TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (source_a <= source_b)
);

ALTER TABLE versus_judgments ENABLE ROW LEVEL SECURITY;

CREATE INDEX idx_versus_judgments_pair ON versus_judgments(essay_id, prefix_hash, source_a, source_b, criterion);
CREATE INDEX idx_versus_judgments_lookup ON versus_judgments(essay_id, prefix_hash, source_a, source_b, criterion, judge_inputs_hash);
CREATE INDEX idx_versus_judgments_project_run ON versus_judgments(project_id, run_id) WHERE project_id IS NOT NULL;
CREATE INDEX idx_versus_judgments_rumil_call ON versus_judgments(rumil_call_id) WHERE rumil_call_id IS NOT NULL;
CREATE INDEX idx_versus_judgments_text_a ON versus_judgments(text_a_id);
CREATE INDEX idx_versus_judgments_text_b ON versus_judgments(text_b_id);
