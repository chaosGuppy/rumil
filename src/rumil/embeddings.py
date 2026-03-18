"""Embedding creation (Voyage AI) and vector search (Supabase pgvector)."""

import logging
from typing import Any, Sequence

from voyageai.client_async import AsyncClient

from rumil.database import DB, _Rows, _row_to_page, _rows
from rumil.models import Page, Workspace
from rumil.settings import get_settings

log = logging.getLogger(__name__)

EMBEDDING_MODEL = "voyage-4-large"
EMBEDDING_DIMENSIONS = 1024


def _get_client() -> AsyncClient:
    key = get_settings().voyage_ai_api_key
    if not key:
        raise EnvironmentError(
            "VOYAGE_AI_API_KEY not set. Add it to .env to use embeddings."
        )
    return AsyncClient(api_key=key)


async def embed_texts(
    texts: list[str],
    input_type: str = "document",
) -> Sequence[Sequence[float]]:
    """Create embeddings for a list of texts via Voyage AI.

    input_type should be "document" for content being stored, or "query"
    for search queries.
    """
    if not texts:
        return []
    client = _get_client()
    result = await client.embed(
        texts,
        model=EMBEDDING_MODEL,
        input_type=input_type,
        output_dimension=EMBEDDING_DIMENSIONS,
    )
    return result.embeddings


async def embed_query(text: str) -> Sequence[float]:
    """Create an embedding for a search query."""
    embeddings = await embed_texts([text], input_type="query")
    return embeddings[0]


def page_text_for_field(page: Page, field_name: str) -> str:
    """Return the text to embed for a given page field."""
    if field_name == "content":
        return f"{page.headline}\n\n{page.content}"
    if field_name == "headline":
        return page.headline
    raise ValueError(f"Unknown embedding field: {field_name}")


async def embed_page(page: Page, field_name: str = "content") -> Sequence[float]:
    """Create an embedding for a page field."""
    text = page_text_for_field(page, field_name)
    embeddings = await embed_texts([text], input_type="document")
    return embeddings[0]


async def store_embedding(
    db: DB,
    page_id: str,
    field_name: str,
    embedding: Sequence[float],
) -> None:
    """Store an embedding vector for a page field (upsert)."""
    embedding_str = '[' + ','.join(str(x) for x in embedding) + ']'
    await db.client.table("page_embeddings").upsert(
        {
            "page_id": page_id,
            "field_name": field_name,
            "embedding": embedding_str,
        },
        on_conflict="page_id,field_name",
    ).execute()
    log.debug("Stored %s embedding for page %s", field_name, page_id[:8])


async def embed_and_store_page(
    db: DB,
    page: Page,
    field_name: str = "content",
) -> None:
    """Create and store an embedding for a page field in one step."""
    embedding = await embed_page(page, field_name=field_name)
    await store_embedding(db, page.id, field_name, embedding)


async def search_pages(
    db: DB,
    query: str,
    match_threshold: float = 0.5,
    match_count: int = 10,
    workspace: Workspace | None = None,
    field_name: str | None = None,
) -> list[tuple[Page, float]]:
    """Search for pages similar to a query string.

    Returns (page, similarity_score) pairs sorted by descending similarity.
    Optionally filter to embeddings of a specific field_name.
    """
    query_embedding = await embed_query(query)
    return await search_pages_by_vector(
        db,
        query_embedding,
        match_threshold=match_threshold,
        match_count=match_count,
        workspace=workspace,
        field_name=field_name,
    )


async def search_pages_by_vector(
    db: DB,
    query_embedding: Sequence[float],
    match_threshold: float = 0.5,
    match_count: int = 10,
    workspace: Workspace | None = None,
    field_name: str | None = None,
) -> list[tuple[Page, float]]:
    """Search for pages similar to a given embedding vector.

    Returns (page, similarity_score) pairs sorted by descending similarity.
    Optionally filter to embeddings of a specific field_name.
    """
    embedding_str = '[' + ','.join(str(x) for x in query_embedding) + ']'
    params: dict[str, Any] = {
        "query_embedding": embedding_str,
        "match_threshold": match_threshold,
        "match_count": match_count,
    }
    if workspace:
        params["filter_workspace"] = workspace.value
    if db.project_id:
        params["filter_project_id"] = db.project_id
    if field_name:
        params["filter_field_name"] = field_name
    rows: _Rows = _rows(await db.client.rpc("match_pages", params).execute())
    results = []
    for row in rows:
        page = _row_to_page(row)
        similarity = row["similarity"]
        results.append((page, similarity))
    return results


async def backfill_embeddings(
    db: DB,
    field_name: str = "content",
    workspace: Workspace | None = None,
    batch_size: int = 50,
) -> int:
    """Generate and store embeddings for pages missing a given field embedding.

    Returns the number of pages embedded.
    """
    params: dict[str, Any] = {
        "p_field_name": field_name,
        "p_limit": batch_size,
    }
    if workspace:
        params["p_workspace"] = workspace.value
    if db.project_id:
        params["p_project_id"] = db.project_id
    rows: _Rows = _rows(
        await db.client.rpc("pages_missing_embedding", params).execute()
    )
    if not rows:
        return 0
    pages = [_row_to_page(r) for r in rows]
    texts = [page_text_for_field(p, field_name) for p in pages]
    embeddings = await embed_texts(texts, input_type="document")
    for page, embedding in zip(pages, embeddings):
        await store_embedding(db, page.id, field_name, embedding)
    log.info("Backfilled %s embeddings for %d pages", field_name, len(pages))
    return len(pages)
