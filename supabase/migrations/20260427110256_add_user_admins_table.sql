-- Admin role: presence in this table grants access to traces, statistics,
-- and ab-eval pages in the web UI. Service-role queries bypass RLS, so the
-- API reads/writes this table directly. Bootstrap with scripts/grant_admin.py
-- or by inserting a row whose user_id matches an auth.users id.
CREATE TABLE user_admins (
    user_id    UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_by UUID REFERENCES auth.users(id) ON DELETE SET NULL,
    note       TEXT
);

ALTER TABLE user_admins ENABLE ROW LEVEL SECURITY;

CREATE INDEX idx_user_admins_granted_at ON user_admins(granted_at DESC);
