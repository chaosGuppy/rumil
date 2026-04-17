-- Drop legacy AB-run plumbing. Staged runs and the ab_branch.sh workflow
-- fully replace the in-process --ab mode that populated these columns.

DROP INDEX IF EXISTS idx_pages_ab_run;
DROP INDEX IF EXISTS idx_page_links_ab_run;
DROP INDEX IF EXISTS idx_runs_ab_run;

ALTER TABLE pages DROP COLUMN IF EXISTS ab_run_id;
ALTER TABLE page_links DROP COLUMN IF EXISTS ab_run_id;
ALTER TABLE runs DROP COLUMN IF EXISTS ab_run_id;
ALTER TABLE runs DROP COLUMN IF EXISTS ab_arm;

DROP TABLE IF EXISTS ab_runs;
