-- Mid-run human steering: typed nudges that shape orchestrator/call behavior.
--
-- One row per nudge. The surface that authored it (parma chat, inline UI,
-- CLI, skill) doesn't matter — every authoring path writes the same row
-- shape and the orchestrator + per-call context build reads them at safe
-- points.
--
-- Composition semantics:
--   * hard=true nudges union into a pre-dispatch filter (bans/vetoes).
--   * soft_text is concatenated newest-first into a "Human steering"
--     context section seen by prioritization and per-call context builds.
--   * one_shot nudges flip status='consumed' after first apply; persistent
--     stay active until revoked or expired.
--
-- Scope is a JSONB bag so we can add new axes without migrations. Today's
-- recognized keys:
--   call_types:             list of CallType enum values this nudge targets
--   question_ids:           list of page ids whose subtree this applies to
--   call_id:                single call id for veto/redo kinds
--   expires_at:             absolute ISO timestamp; past it, status flips to 'expired'
--   expires_after_n_calls:  int; decremented as nudge gets applied
--
-- Staging: staged + run_id work the same way as annotation_events and
-- reputation_events. stage_run() snapshots active nudges into the staged
-- run's view; nudges authored on a staged run stay isolated.

CREATE TABLE run_nudges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    author_kind TEXT NOT NULL CHECK (author_kind IN ('human', 'claude', 'system')),
    author_note TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK (kind IN (
        'constrain_dispatch',
        'inject_note',
        'rewrite_goal',
        'veto_call',
        'redo_call',
        'pause'
    )),
    payload JSONB NOT NULL DEFAULT '{}',
    durability TEXT NOT NULL CHECK (durability IN ('one_shot', 'persistent')),
    scope JSONB NOT NULL DEFAULT '{}',
    soft_text TEXT,
    hard BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'expired', 'revoked', 'consumed')),
    staged BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ,
    consumed_at TIMESTAMPTZ
);

-- Hot path: the orchestrator asks "what's active on this run?" before
-- every dispatch decision. Partial index keeps this cheap even as the
-- table accretes expired/consumed/revoked rows over long-running runs.
CREATE INDEX idx_run_nudges_active
    ON run_nudges (run_id)
    WHERE status = 'active';

CREATE INDEX idx_run_nudges_staged
    ON run_nudges (staged)
    WHERE staged = TRUE;

ALTER TABLE run_nudges ENABLE ROW LEVEL SECURITY;
