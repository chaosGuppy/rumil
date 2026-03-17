"""Embedding creation (Voyage AI) and vector search (Supabase pgvector)."""

import logging
from typing import Any

import voyageai

from rumil.database import DB, _Rows, _row_to_page, _rows
from rumil.models import Page, Workspace
from rumil.settings import get_settings

log = logging.getLogger(__name__)

EMBEDDING_MODEL = "voyage-4-large"
EMBEDDING_DIMENSIONS = 1024


def _get_client() -> voyageai.AsyncClient:
    key = get_settings().voyage_ai_api_key
    if not key:
        raise EnvironmentError(
            "VOYAGE_AI_API_KEY not set. Add it to .env to use embeddings."
        )
    return voyageai.AsyncClient(api_key=key)


async def embed_texts(
    texts: list[str],
    input_type: str = "document",
) -> list[list[float]]:
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


async def embed_query(text: str) -> list[float]:
    """Create an embedding for a search query."""
    embeddings = await embed_texts([text], input_type="query")
    return embeddings[0]


async def embed_page(page: Page) -> list[float]:
    """Create an embedding for a page's content."""
    text = f"{page.summary}\n\n{page.content}"
    embeddings = await embed_texts([text], input_type="document")
    return embeddings[0]


async def store_embedding(db: DB, page_id: str, embedding: list[float]) -> None:
    """Store an embedding vector for a page."""
    embedding_str = '[' + ','.join(str(x) for x in embedding) + ']'
    await db.client.table("pages").update(
        {"embedding": embedding_str}
    ).eq("id", page_id).execute()
    log.debug("Stored embedding for page %s", page_id[:8])


async def embed_and_store_page(db: DB, page: Page) -> None:
    """Create and store an embedding for a page in one step."""
    embedding = await embed_page(page)
    await store_embedding(db, page.id, embedding)


async def search_pages(
    db: DB,
    query: str,
    match_threshold: float = 0.5,
    match_count: int = 10,
    workspace: Workspace | None = None,
) -> list[tuple[Page, float]]:
    """Search for pages similar to a query string.

    Returns (page, similarity_score) pairs sorted by descending similarity.
    """
    query_embedding = await embed_query(query)
    return await search_pages_by_vector(
        db,
        query_embedding,
        match_threshold=match_threshold,
        match_count=match_count,
        workspace=workspace,
    )


async def search_pages_by_vector(
    db: DB,
    query_embedding: list[float],
    match_threshold: float = 0.5,
    match_count: int = 10,
    workspace: Workspace | None = None,
) -> list[tuple[Page, float]]:
    """Search for pages similar to a given embedding vector.

    Returns (page, similarity_score) pairs sorted by descending similarity.
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
    rows: _Rows = _rows(await db.client.rpc("match_pages", params).execute())
    results = []
    for row in rows:
        page = _row_to_page(row)
        similarity = row["similarity"]
        results.append((page, similarity))
    return results


async def backfill_embeddings(
    db: DB,
    workspace: Workspace | None = None,
    batch_size: int = 50,
) -> int:
    """Generate and store embeddings for all pages that don't have one yet.

    Returns the number of pages embedded.
    """
    query = (
        db.client.table("pages")
        .select("*")
        .is_("embedding", "null")
        .eq("is_superseded", False)
    )
    if workspace:
        query = query.eq("workspace", workspace.value)
    if db.project_id:
        query = query.eq("project_id", db.project_id)
    rows: _Rows = _rows(await query.limit(batch_size).execute())
    if not rows:
        return 0
    pages = [_row_to_page(r) for r in rows]
    texts = [f"{p.summary}\n\n{p.content}" for p in pages]
    embeddings = await embed_texts(texts, input_type="document")
    for page, embedding in zip(pages, embeddings):
        await store_embedding(db, page.id, embedding)
    log.info("Backfilled embeddings for %d pages", len(pages))
    return len(pages)
