-- Add task_shape JSONB column to pages table (v1 task-shape taxonomy).
-- task_shape is applied only to page_type='question' rows; non-question
-- pages always have NULL. Schema and tagging policy live in
-- marketplace-thread/27-task-shape-taxonomy.md.

ALTER TABLE pages ADD COLUMN task_shape JSONB NULL;

CREATE INDEX IF NOT EXISTS idx_pages_task_shape ON pages USING gin (task_shape);
