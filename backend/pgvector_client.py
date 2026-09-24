"""
pgvector_client.py — PostgreSQL + pgvector integration (Confluence KB).

Replaces chroma_client.py. This is the ONLY module tools.py talks to for
knowledge-base search — everything else (embeddings.py, kb_store.py,
kb_retrieval.py) is an implementation detail contained behind this
module, same role chroma_client.py used to play.

WHAT CHANGED FROM CHROMADB:
  - Vector storage/search moved from a local ChromaDB (TF-IDF-embedded,
    POC) to a production PostgreSQL + pgvector table (or tables),
    embedded with Azure OpenAI's text-embedding-3-large.
  - Query embedding now happens via a real embedding model
    (embeddings.py) instead of a saved TF-IDF vectorizer — so natural,
    descriptive queries work well here (unlike the old TF-IDF setup,
    full sentences are fine, not just keyword phrases).
  - Retrieval ranking (title-match short-circuit, page-then-chunk
    scoring, noise filtering) is handled by kb_retrieval.py, built by
    the ingestion team specifically for this table schema.

MULTIPLE CONFLUENCE SOURCES:
  config.KB_PG_TABLES is a list. Every table in it is searched on each
  search() call; results are merged and re-ranked before the top_k are
  returned — same behavior the old CHROMA_COLLECTION_NAMES list gave us.
  Title-match hits (kb_retrieval short-circuits to a page's intro chunks
  when the query names that page) are treated as highest priority,
  ahead of any distance-ranked hit from another table.

CACHING:
  Search results are cached in-memory, keyed by (query, top_k, tables),
  for config.KB_CACHE_TTL_SECONDS — same rationale as the old Chroma
  cache: avoid repeating the embedding-API call + vector search for a
  repeated/near-duplicate question within a short window.
"""
import time
import logging
from typing import Optional

import config
import kb_store
import kb_retrieval

logger = logging.getLogger("pgvector_client")

_CACHE: dict = {}   # cache_key -> (timestamp, results)


def is_available() -> bool:
    """Best-effort check used by /readyz — never raises."""
    try:
        return kb_store.is_available()
    except Exception:
        return False


def chunk_count() -> Optional[int]:
    """Total rows across every configured KB table, for startup logging."""
    try:
        total = 0
        for table in config.KB_PG_TABLES:
            total += kb_store.count_rows(table)
        return total
    except Exception:
        return None


def _normalize(hit: dict, table: str, title_match: bool) -> dict:
    """Uniform shape across both retrieval paths (title-match intro
    chunks have no `distance`/`similarity`; vector-search hits do)."""
    return {
        "chunk": hit.get("chunk"),
        "page_id": hit.get("page_id"),
        "page_title": hit.get("page_title"),
        "section_id": hit.get("section_id"),
        "chunk_id": hit.get("chunk_id"),
        "metadata": hit.get("metadata") or {},
        "page_version": hit.get("page_version"),
        "distance": hit.get("distance"),  # None for title-match hits
        "source_table": table,
        "title_match": title_match,
    }


def search(query: str, top_k: Optional[int] = None, where: Optional[dict] = None):
    """
    Retrieve the most relevant Confluence chunks for `query` across every
    table in config.KB_PG_TABLES, merged and ranked.

    Args:
        query: natural-language query text. Unlike the old TF-IDF setup,
            full descriptive questions work well here — no need to strip
            down to keywords, since this uses real semantic embeddings.
        top_k: number of chunks to return, across ALL tables combined.
            Defaults to config.KB_TOP_K.
        where: NOT currently supported by the underlying store (the
            ingestion team's schema doesn't expose a metadata filter
            path yet). Accepted for interface compatibility only; a
            non-empty value is logged and ignored rather than raising,
            so callers don't need special-case handling.

    Returns a list of dicts: [{"chunk", "page_id", "page_title",
    "section_id", "chunk_id", "metadata", "page_version", "distance",
    "source_table", "title_match"}, ...], ordered most-relevant-first
    (title-match hits first, then by ascending distance). Raises
    RuntimeError if the KB store isn't reachable at all.
    """
    if where:
        logger.warning(
            "[pgvector_client] `where` filter %r was passed but isn't "
            "supported by the current KB schema — ignoring it.", where
        )

    n_results = top_k or config.KB_TOP_K
    tables = tuple(config.KB_PG_TABLES)

    cache_key = (query, n_results, tables)
    cached = _CACHE.get(cache_key)
    if cached and (time.time() - cached[0]) < config.KB_CACHE_TTL_SECONDS:
        return cached[1]

    if not is_available():
        raise RuntimeError(
            kb_store.init_error()
            or "PostgreSQL KB store is not reachable. Confirm KB_PG_HOST/"
               "KB_PG_DB/KB_PG_USER/KB_PG_PASSWORD are set correctly."
        )

    merged = []
    errors = []
    for table in tables:
        try:
            hits = kb_retrieval.retrieve_context(
                query,
                embedding_api_key=config.AZURE_OPENAI_EMBEDDING_API_KEY,
                db_table=table,
                initial_top_k=config.KB_INITIAL_TOP_K,
                top_pages=config.KB_TOP_PAGES,
                chunks_per_page=config.KB_CHUNKS_PER_PAGE,
            )
        except Exception as e:
            logger.warning("[pgvector_client] retrieval failed on table '%s': %s", table, e)
            errors.append(str(e))
            continue

        # If a hit has no "distance" key, kb_retrieval short-circuited to
        # a title match (get_page_intro_chunks) for this table's query.
        for hit in hits:
            merged.append(_normalize(hit, table, title_match="distance" not in hit))

    if not merged and errors:
        raise RuntimeError(
            f"KB search failed on every configured table: {' | '.join(errors)}"
        )

    # Title matches first (forced-priority, exact page-name hits), then
    # everything else by ascending distance (closer = more relevant).
    merged.sort(
        key=lambda r: (
            0 if r["title_match"] else 1,
            r["distance"] if r["distance"] is not None else float("inf"),
        )
    )
    results = merged[:n_results]

    _CACHE[cache_key] = (time.time(), results)
    return results
