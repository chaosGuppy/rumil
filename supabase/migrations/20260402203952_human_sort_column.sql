ALTER TABLE pages
  ADD COLUMN is_human_created boolean
  GENERATED ALWAYS AS (provenance_model = 'human') STORED;
