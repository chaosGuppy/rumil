-- Replace IVFFlat index with HNSW for reliable recall.
-- IVFFlat with default probes=1 searches only 1 of 100 lists,
-- causing ~99% of similar vectors to be invisible.

DROP INDEX IF EXISTS idx_page_embeddings_vector;

CREATE INDEX idx_page_embeddings_vector ON page_embeddings
    USING hnsw (embedding extensions.vector_cosine_ops);
