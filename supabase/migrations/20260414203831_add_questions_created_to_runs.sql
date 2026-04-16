ALTER TABLE runs ADD COLUMN questions_created INTEGER NOT NULL DEFAULT 0;

CREATE OR REPLACE FUNCTION increment_questions_created(p_run_id TEXT)
RETURNS VOID
LANGUAGE sql
AS $$
  UPDATE runs
  SET questions_created = questions_created + 1
  WHERE id = p_run_id;
$$;
