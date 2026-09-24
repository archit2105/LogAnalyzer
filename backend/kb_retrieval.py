"""
kb_retrieval.py — Confluence KB retrieval over PostgreSQL + pgvector.

Adapted from the ingestion team's retrieval (1).py. The ranking logic
(title-match short-circuit, page-then-chunk hierarchical scoring, noise
filtering) is unchanged from what they built and tuned — the only
changes here are import paths (flat module imports instead of a
relative package layout, to match this backend's existing style) and
defaults sourced from config.py instead of being hardcoded.
"""
import re
import time

from embeddings import get_embedding
from kb_store import query_chunks, list_page_titles, get_page_intro_chunks

import config

DEFAULT_INITIAL_TOP_K = config.KB_INITIAL_TOP_K
DEFAULT_TOP_PAGES = config.KB_TOP_PAGES
DEFAULT_CHUNKS_PER_PAGE = config.KB_CHUNKS_PER_PAGE
AVG_SIMILARITY_WEIGHT = 0.3
TITLE_BONUS_PER_TERM = 0.05
TITLE_BONUS_MAX = 0.15
TITLE_MATCH_RATIO_THRESHOLD = 0.7
TITLE_MATCH_BONUS = 1.0
MIN_CHUNK_LENGTH = 20
NOISE_VALUES = {"n/a", "na", "none", "n / a"}
PAGE_TITLE_CACHE_TTL_SECONDS = 300

WORD_PATTERN = re.compile(r"[a-z0-9]+")

_page_title_cache = {}


def _cached_page_titles(db_table):
    """Page titles rarely change between chat turns, so cache them per table
    instead of re-fetching every page on every query (find_title_match used to
    do a full table scan before embedding search even ran)."""

    cached = _page_title_cache.get(db_table)
    now = time.monotonic()
    if cached and now - cached[0] < PAGE_TITLE_CACHE_TTL_SECONDS:
        return cached[1]

    titles = list_page_titles(db_table)
    _page_title_cache[db_table] = (now, titles)
    return titles


def invalidate_page_title_cache(db_table=None):
    if db_table is None:
        _page_title_cache.clear()
    else:
        _page_title_cache.pop(db_table, None)


PAREN_PATTERN = re.compile(r"\([^)]*\)")


def _terms(text):
    """Strip parenthetical codes (e.g. "(coic_lqa_1001)") before tokenizing,
    so they don't drag down the title/query overlap ratio for users who type
    the descriptive part of a title without its trailing technical code."""

    text = PAREN_PATTERN.sub(" ", text)
    return {term for term in WORD_PATTERN.findall(text.lower()) if len(term) > 2}


def is_noise_chunk(chunk):
    """Filter out empty, too-short, or placeholder chunks (e.g. leftover
    Confluence macro artifacts that collapse to "N/A" once rendered as text)."""

    text = chunk.strip()

    if len(text) < MIN_CHUNK_LENGTH:
        return True

    if text.lower() in NOISE_VALUES:
        return True

    return False


def title_bonus(query_terms, title_terms):
    matches = len(query_terms & title_terms)
    return min(matches * TITLE_BONUS_PER_TERM, TITLE_BONUS_MAX)


def title_match_ratio(query_terms, title_terms):
    """How much the query and the page title overlap, in both directions.
    Near 1.0 means the query is essentially the page title (e.g. the user
    searched by heading), in which case they're after that page's intro
    rather than whichever chunk happens to embed closest."""

    if not query_terms or not title_terms:
        return 0.0

    overlap = len(query_terms & title_terms)
    return min(overlap / len(title_terms), overlap / len(query_terms))


def find_title_match(query, db_table):
    """Look for a page whose title the query essentially names, independent of
    embedding search. A page can miss the initial ANN top-k even when the query
    is literally its title, so this checks against every known page title
    directly instead of relying on vector-search recall."""

    query_terms = _terms(query)

    if not query_terms:
        return None

    best_page = None
    best_ratio = 0.0

    for page in _cached_page_titles(db_table):
        ratio = title_match_ratio(query_terms, _terms(page["page_title"]))
        if ratio > best_ratio:
            best_ratio = ratio
            best_page = page

    if best_page and best_ratio >= TITLE_MATCH_RATIO_THRESHOLD:
        return best_page

    return None


def select_top_chunks(
    query,
    candidates,
    top_pages=DEFAULT_TOP_PAGES,
    chunks_per_page=DEFAULT_CHUNKS_PER_PAGE
):
    """Group chunks by page and score each page as a blend of its best-matching
    chunk and its average chunk similarity, with a bonus when query terms appear
    in the page title. If the query is essentially the page title, that page is
    forced to the top and its intro chunks (earliest section/chunk) are returned
    instead of whichever chunks happen to embed closest."""

    query_terms = _terms(query)

    pages = {}
    for candidate in candidates:
        if is_noise_chunk(candidate["chunk"]):
            continue
        candidate["similarity"] = 1 - candidate["distance"]
        pages.setdefault(candidate["page_id"], []).append(candidate)

    scored_pages = []
    for page_id, chunks in pages.items():
        title_terms = _terms(chunks[0]["page_title"])
        similarities = [chunk["similarity"] for chunk in chunks]
        max_similarity = max(similarities)
        avg_similarity = sum(similarities) / len(similarities)
        bonus = title_bonus(query_terms, title_terms)
        is_title_match = title_match_ratio(query_terms, title_terms) >= TITLE_MATCH_RATIO_THRESHOLD
        page_score = max_similarity + AVG_SIMILARITY_WEIGHT * avg_similarity + bonus
        if is_title_match:
            page_score += TITLE_MATCH_BONUS
        scored_pages.append((page_score, page_id, chunks, is_title_match))

    scored_pages.sort(key=lambda item: item[0], reverse=True)

    selected = []
    for page_score, page_id, chunks, is_title_match in scored_pages[:top_pages]:
        if is_title_match:
            top_chunks = sorted(
                chunks,
                key=lambda chunk: (chunk["section_id"], chunk["chunk_id"])
            )[:chunks_per_page]
        else:
            top_chunks = sorted(
                chunks,
                key=lambda chunk: chunk["similarity"],
                reverse=True
            )[:chunks_per_page]
        selected.extend(top_chunks)

    return selected


def retrieve_context(
    query,
    embedding_api_key,
    db_table,
    initial_top_k=DEFAULT_INITIAL_TOP_K,
    top_pages=DEFAULT_TOP_PAGES,
    chunks_per_page=DEFAULT_CHUNKS_PER_PAGE
):
    """If the query essentially names a page title, fetch that page's intro
    chunks directly and skip embedding search entirely. Otherwise embed the
    query, vector-search chunks, then narrow to the best chunks via a
    hierarchical page-then-chunk ranking: score and select the top pages, and
    keep only the best-matching chunks within each."""

    title_match = find_title_match(query, db_table)

    if title_match:
        return get_page_intro_chunks(
            page_id=title_match["page_id"],
            db_table=db_table,
            limit=chunks_per_page
        )

    query_vector = get_embedding(
        query,
        api_key=embedding_api_key
    )

    candidates = query_chunks(
        query_vector=query_vector,
        db_table=db_table,
        top_k=initial_top_k
    )

    return select_top_chunks(
        query=query,
        candidates=candidates,
        top_pages=top_pages,
        chunks_per_page=chunks_per_page
    )
