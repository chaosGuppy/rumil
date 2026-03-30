ALTER TABLE epistemic_scores ADD COLUMN source_page_id TEXT REFERENCES pages(id);
