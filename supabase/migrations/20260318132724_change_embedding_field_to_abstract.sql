-- Rename embedding field_name from 'headline' to 'abstract'.
UPDATE page_embeddings SET field_name = 'abstract' WHERE field_name = 'headline';
