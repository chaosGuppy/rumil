-- Add a generated word-count column to versus_texts so the /results
-- aggregator can read it without shipping the full text blob over the
-- wire. The text column is the dominant payload size on iter_texts
-- (multi-MB across all rows) and the only thing /results needs from it
-- is len(text.split()) for source-stats avg_words.
--
-- regexp_split_to_array on \s+ matches Python's str.split() — splits on
-- any whitespace, collapses runs. Wrapped in CASE so empty / NULL text
-- doesn't crash array_length.

ALTER TABLE versus_texts ADD COLUMN response_words INT GENERATED ALWAYS AS (
    CASE
        WHEN text IS NULL OR length(trim(text)) = 0 THEN 0
        ELSE coalesce(array_length(regexp_split_to_array(trim(text), '\s+'), 1), 0)
    END
) STORED;
