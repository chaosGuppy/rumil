-- Attach projects to Supabase Auth users. Nullable so pre-auth projects
-- remain readable from the CLI (which can stamp its own default owner via
-- DEFAULT_CLI_USER_ID). The authed API layer filters out NULL-owner projects
-- for authenticated users, so existing projects are invisible from the web
-- UI until explicitly backfilled.
ALTER TABLE projects
    ADD COLUMN owner_user_id UUID REFERENCES auth.users(id) ON DELETE SET NULL;

CREATE INDEX idx_projects_owner_user_id ON projects(owner_user_id);
