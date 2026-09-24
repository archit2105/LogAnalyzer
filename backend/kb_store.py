"""
kb_store.py — PostgreSQL + pgvector access for the Confluence knowledge
base. READ-ONLY: this app never chunks, embeds, or inserts anything —
that pipeline lives wherever the Confluence ingestion job runs. This
module only queries what that job already wrote.

Adapted from the ingestion team's database_insertion.py, keeping just the
read-path functions kb_retrieval.py actually needs (query_chunks,
list_page_titles, get_page_intro_chunks) and dropping the
insertion/chunking-pipeline functions (insert_chunks, get_max_chunk_id,
list_page_versions) that belong to the ingestion job, not this app.

Table shape expected (per table in config.KB_PG_TABLES), matching what
the ingestion job writes:
    id, page_id, page_title, section, section_id, chunk, chunk_id,
    embedding (vector), source, metadata (jsonb), page_version, created_at
"""
from contextlib import contextmanager

import psycopg2
from psycopg2 import pool as psycopg2_pool
from psycopg2 import sql
from pgvector import Vector
from pgvector.psycopg2 import register_vector

import config

_connection_pool = None
_INIT_ERROR = None


def _connection_params():
    if not all([config.KB_PG_HOST, config.KB_PG_DB, config.KB_PG_USER, config.KB_PG_PASSWORD]):
        raise ValueError(
            "Missing PostgreSQL configuration for the KB store. Set "
            "KB_PG_HOST, KB_PG_DB, KB_PG_USER, and KB_PG_PASSWORD "
            "(or the shared PG_HOST/PG_DB/PG_USER/PG_PASSWORD fallback) "
            "in the environment."
        )

    return {
        "host": config.KB_PG_HOST,
        "port": config.KB_PG_PORT,
        "dbname": config.KB_PG_DB,
        "user": config.KB_PG_USER,
        "password": config.KB_PG_PASSWORD,
    }


def _get_pool():
    # Reused across queries so each chat turn doesn't pay a fresh TCP/TLS
    # handshake to Postgres on top of the actual query cost.
    global _connection_pool, _INIT_ERROR
    if _connection_pool is None:
        params = _connection_params()  # raises ValueError if unset
        _connection_pool = psycopg2_pool.ThreadedConnectionPool(
            config.KB_PG_POOL_MIN,
            config.KB_PG_POOL_MAX,
            **params,
        )
    return _connection_pool


@contextmanager
def _pooled_connection():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        conn.autocommit = True
        register_vector(conn)
        yield conn
    finally:
        pool.putconn(conn)


def is_available() -> bool:
    """
    Best-effort check used by /readyz and startup logging — never
    raises. True if the pool can be created AND at least one configured
    table is queryable.
    """
    global _INIT_ERROR
    try:
        with _pooled_connection() as connection:
            with connection.cursor() as cursor:
                for table in config.KB_PG_TABLES:
                    try:
                        cursor.execute(
                            sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier(table))
                        )
                        return True
                    except Exception:
                        continue
        _INIT_ERROR = (
            f"None of the configured KB tables ({config.KB_PG_TABLES}) "
            f"could be queried."
        )
        return False
    except Exception as e:
        _INIT_ERROR = str(e)
        return False


def init_error() -> str:
    return _INIT_ERROR or ""


def query_chunks(query_vector, db_table, top_k=50):
    """
    Nearest-neighbor search over `db_table` using pgvector's cosine
    distance operator (<=>). Lower distance = closer match.
    """
    with _pooled_connection() as connection:
        with connection.cursor() as cursor:
            select_query = sql.SQL(
                "SELECT chunk, page_id, page_title, section_id, chunk_id, metadata, page_version, embedding <=> %s AS distance FROM {} "
                "ORDER BY distance LIMIT %s"
            ).format(sql.Identifier(db_table))
            cursor.execute(select_query, [Vector(query_vector), top_k])
            rows = cursor.fetchall()

    return [
        {
            "chunk": chunk,
            "page_id": page_id,
            "page_title": page_title,
            "section_id": section_id,
            "chunk_id": chunk_id,
            "metadata": metadata,
            "page_version": page_version,
            "distance": float(distance)
        }
        for chunk, page_id, page_title, section_id, chunk_id, metadata, page_version, distance in rows
    ]


def list_page_titles(db_table):
    with _pooled_connection() as connection:
        with connection.cursor() as cursor:
            select_query = sql.SQL(
                "SELECT DISTINCT page_id, page_title FROM {}"
            ).format(sql.Identifier(db_table))
            cursor.execute(select_query)
            rows = cursor.fetchall()

    return [
        {"page_id": page_id, "page_title": page_title}
        for page_id, page_title in rows
    ]


def get_page_intro_chunks(page_id, db_table, limit=2):
    """
    The first `limit` chunks of a page (by section_id, chunk_id order) —
    used when the query essentially names the page's title, so we return
    that page's intro instead of whichever chunk happens to embed
    closest to the (very short, title-like) query text.
    """
    with _pooled_connection() as connection:
        with connection.cursor() as cursor:
            select_query = sql.SQL(
                "SELECT chunk, page_id, page_title, section_id, chunk_id, metadata, page_version FROM {} "
                "WHERE page_id = %s ORDER BY section_id, chunk_id LIMIT %s"
            ).format(sql.Identifier(db_table))
            cursor.execute(select_query, [page_id, limit])
            rows = cursor.fetchall()

    return [
        {
            "chunk": chunk,
            "page_id": page_id,
            "page_title": page_title,
            "section_id": section_id,
            "chunk_id": chunk_id,
            "metadata": metadata,
            "page_version": page_version
        }
        for chunk, page_id, page_title, section_id, chunk_id, metadata, page_version in rows
    ]


def count_rows(db_table) -> int:
    """Total row count for a table — used for startup logging only."""
    with _pooled_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(db_table))
            )
            (count,) = cursor.fetchone()
    return count


def close_pool() -> None:
    global _connection_pool
    if _connection_pool is not None:
        _connection_pool.closeall()
        _connection_pool = None
