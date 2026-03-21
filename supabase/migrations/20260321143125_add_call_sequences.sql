CREATE TABLE call_sequences (
    id TEXT PRIMARY KEY,
    parent_call_id TEXT REFERENCES calls(id),
    run_id TEXT NOT NULL,
    scope_question_id TEXT REFERENCES pages(id),
    position_in_batch INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_call_sequences_parent ON call_sequences(parent_call_id);
CREATE INDEX idx_call_sequences_run ON call_sequences(run_id);
ALTER TABLE call_sequences ENABLE ROW LEVEL SECURITY;
CREATE POLICY "allow all" ON call_sequences FOR ALL USING (true) WITH CHECK (true);

ALTER TABLE calls ADD COLUMN sequence_id TEXT REFERENCES call_sequences(id);
ALTER TABLE calls ADD COLUMN sequence_position INT;
CREATE INDEX idx_calls_sequence ON calls(sequence_id);
