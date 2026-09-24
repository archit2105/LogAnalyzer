"""
config.py — central configuration for the production integration.

Values can be overridden by environment variables before app startup.

PRODUCTION NOTE: this app now connects directly to live New Relic logs
(no more synthetic-log file backend, no more logtype scoping). The New
Relic Account ID and API Key are environment-specific and must be supplied
via env vars / secrets manager — there is no baked-in demo default anymore.
"""
import os

# --------------------------------------------------------------------------
# New Relic
# --------------------------------------------------------------------------
# Required. No defaults — set these via env vars or a secrets manager for
# this environment. (A new Account ID and API Key were issued for the
# production integration; the old POC demo credentials are retired.)
NEW_RELIC_API_KEY = os.environ.get("NEW_RELIC_API_KEY")
NEW_RELIC_ACCOUNT_ID = os.environ.get("NEW_RELIC_ACCOUNT_ID")

# US endpoint by default. Set NEW_RELIC_REGION=EU to use the EU endpoint.
_REGION = os.environ.get("NEW_RELIC_REGION", "US").upper()
NEW_RELIC_GRAPHQL_ENDPOINT = (
    "https://api.eu.newrelic.com/graphql" if _REGION == "EU"
    else "https://api.newrelic.com/graphql"
)

# --------------------------------------------------------------------------
# Log scope — no logtype restriction anymore. All log types across the
# platform's services are in scope. Logs come from two architectural
# components, distinguished by these fixed filters:
#
#   1. Mulesoft applications — service name starts with "phm-XX-prod".
#      Unique tracking field: `event`.
#   2. AWS Lambda functions   — identified by `faas.arn` containing the
#      account id below. Unique tracking field: `correlationId`.
#
# These patterns are fixed by the platform's naming convention, not
# user input, but are still routed through config so they're one place
# to update if the convention changes.
# --------------------------------------------------------------------------
NEW_RELIC_MULESOFT_SERVICE_LIKE = os.environ.get(
    "NEW_RELIC_MULESOFT_SERVICE_LIKE", "phm%prod"
)
NEW_RELIC_LAMBDA_ARN_LIKE = os.environ.get(
    "NEW_RELIC_LAMBDA_ARN_LIKE", "%974724840714%"
)
# Extra service matched verbatim in the Step-1 failure search (kept as its
# own setting in case additional exact-match services are added later).
NEW_RELIC_EXTRA_EXACT_SERVICES = [
    s.strip() for s in os.environ.get("NEW_RELIC_EXTRA_EXACT_SERVICES", "cquence").split(",")
    if s.strip()
]

# Step 1 (failure discovery) default lookback window.
NEW_RELIC_STEP1_DEFAULT_SINCE = os.environ.get("NEW_RELIC_STEP1_DEFAULT_SINCE", "10 days ago")

# Step 2 (correlation / full-payload fetch) default lookback window.
NEW_RELIC_STEP2_DEFAULT_SINCE = os.environ.get("NEW_RELIC_STEP2_DEFAULT_SINCE", "90 days ago")

# Max records to ask New Relic for in one query. NRQL's own hard cap for
# LIMIT MAX is 2000 records per query.
NEW_RELIC_MAX_LIMIT = int(os.environ.get("NEW_RELIC_MAX_LIMIT", "2000"))


# --------------------------------------------------------------------------
# Azure OpenAI
# --------------------------------------------------------------------------
# Four values needed. Get them from the Azure Portal / Azure OpenAI Studio:
#
#   AZURE_OPENAI_ENDPOINT    - The full URL of your Azure OpenAI resource,
#                              e.g. https://my-openai-resource.openai.azure.com
#                              (No trailing slash needed; we strip it.)
#   AZURE_OPENAI_API_KEY     - The API key ("Key 1" or "Key 2") shown under
#                              your resource's "Keys and Endpoint" tab.
#   AZURE_OPENAI_DEPLOYMENT  - The name of the *deployment* your admin
#                              created in Azure OpenAI Studio → Deployments.
#                              This is NOT the model name; it's a custom
#                              name assigned when the deployment was set up
#                              (e.g. "gpt-4o", "ctx-triage-gpt4"). Ask the
#                              Azure admin if you're unsure.
#   AZURE_OPENAI_API_VERSION - The Azure OpenAI API version to use, e.g.
#                              "2024-08-01-preview". Different versions
#                              support different features (parallel tool
#                              calls, structured outputs, etc.). Match what
#                              your admin recommends.
AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT")
AZURE_OPENAI_API_VERSION = os.environ.get(
    "AZURE_OPENAI_API_VERSION", "2024-08-01-preview"
)

# Strip whitespace on every value — a very common paste error is
# `$env:AZURE_OPENAI_API_KEY = " abc..."` with a leading space, which the
# openai SDK then forwards into the Authorization header and httpx rejects.
if AZURE_OPENAI_ENDPOINT:
    AZURE_OPENAI_ENDPOINT = AZURE_OPENAI_ENDPOINT.strip().rstrip("/")
if AZURE_OPENAI_API_KEY:
    AZURE_OPENAI_API_KEY = AZURE_OPENAI_API_KEY.strip()
if AZURE_OPENAI_DEPLOYMENT:
    AZURE_OPENAI_DEPLOYMENT = AZURE_OPENAI_DEPLOYMENT.strip()
if AZURE_OPENAI_API_VERSION:
    AZURE_OPENAI_API_VERSION = AZURE_OPENAI_API_VERSION.strip()


# --------------------------------------------------------------------------
# PostgreSQL + pgvector (Confluence knowledge base) — production setup
#
# Replaces the old ChromaDB POC integration. Confluence pages are chunked
# and embedded (text-embedding-3-large) by a separate ingestion job into
# a Postgres table with a `pgvector` column. This app ONLY reads/queries
# that table — it doesn't chunk, embed, or ingest anything itself.
#
# This can be the SAME Postgres server as CCM/COIC, or a separate one —
# hence its own connection env vars rather than reusing CCM_DB_*/COIC_DB_*.
# --------------------------------------------------------------------------
KB_PG_HOST = os.environ.get("KB_PG_HOST", os.environ.get("PG_HOST", "")).strip()
KB_PG_PORT = int(os.environ.get("KB_PG_PORT", os.environ.get("PG_PORT", "5432")))
KB_PG_DB = os.environ.get("KB_PG_DB", os.environ.get("PG_DB", "")).strip()
KB_PG_USER = os.environ.get("KB_PG_USER", os.environ.get("PG_USER", "")).strip()
KB_PG_PASSWORD = os.environ.get("KB_PG_PASSWORD", os.environ.get("PG_PASSWORD", ""))

# The table holding chunked, embedded Confluence content (id, page_id,
# page_title, section, section_id, chunk, chunk_id, embedding, source,
# metadata, page_version, created_at). Comma-separate multiple table
# names to search more than one Confluence source/ingestion batch in a
# single query — same "search everything, merge, re-rank" behavior the
# old CHROMA_COLLECTION_NAMES list gave us.
_kb_tables_env = os.environ.get("KB_PG_TABLES", "").strip()
if _kb_tables_env:
    KB_PG_TABLES = [t.strip() for t in _kb_tables_env.split(",") if t.strip()]
else:
    KB_PG_TABLES = [os.environ.get("KB_PG_TABLE", "confluence_docs").strip()]

KB_PG_POOL_MIN = int(os.environ.get("KB_PG_POOL_MIN", "1"))
KB_PG_POOL_MAX = int(os.environ.get("KB_PG_POOL_MAX", "5"))

KB_TOP_K = int(os.environ.get("KB_TOP_K", "8"))
# How many chunks to pull from pgvector before page-level re-ranking
# (kb_retrieval.select_top_chunks narrows this down further).
KB_INITIAL_TOP_K = int(os.environ.get("KB_INITIAL_TOP_K", "30"))
KB_TOP_PAGES = int(os.environ.get("KB_TOP_PAGES", "3"))
KB_CHUNKS_PER_PAGE = int(os.environ.get("KB_CHUNKS_PER_PAGE", "2"))

# TTL for cached KB search results, keyed by the exact (query, top_k,
# tables) tuple. Cuts redundant embedding-API + vector-search work when
# the same/similar questions recur in a session.
KB_CACHE_TTL_SECONDS = int(os.environ.get("KB_CACHE_TTL_SECONDS", "120"))

# --------------------------------------------------------------------------
# Azure OpenAI embeddings — used to embed queries before vector search.
# Must match whatever the ingestion job used to embed the stored chunks
# (text-embedding-3-large) or query vectors won't be comparable.
# --------------------------------------------------------------------------
AZURE_OPENAI_EMBEDDING_ENDPOINT = os.environ.get("AZURE_OPENAI_EMBEDDING_ENDPOINT", "").strip()
# Falls back to the main chat API key if a dedicated one isn't set — set
# AZURE_OPENAI_EMBEDDING_API_KEY explicitly if embeddings live on a
# different Azure OpenAI resource than the chat deployment.
AZURE_OPENAI_EMBEDDING_API_KEY = os.environ.get(
    "AZURE_OPENAI_EMBEDDING_API_KEY", os.environ.get("AZURE_OPENAI_API_KEY", "")
).strip()
AZURE_OPENAI_EMBEDDING_DEPLOYMENT = os.environ.get(
    "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large"
).strip()


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------
NRQL_CACHE_TTL_SECONDS = int(os.environ.get("NRQL_CACHE_TTL_SECONDS", "120"))
# (KB_CACHE_TTL_SECONDS for Confluence/pgvector search results is defined
# above, alongside the rest of the KB config.)


# --------------------------------------------------------------------------
# PostgreSQL (cart metadata) — direct connection, TWO data sources
# --------------------------------------------------------------------------
# Cart metadata now comes from PostgreSQL directly (psycopg/asyncpg over
# port 5432), not Supabase's REST API. There are TWO separate Postgres
# data sources, each with its own connection details:
#
#   CCM  (also known as COM NA)
#   COIC
#
# Which one to query for a given cart/milestone is decided by
# db_client.resolve_milestone_source() — see that module for the full
# CCM vs COIC vs New-Relic milestone mapping.
#
# Each source needs: host, port, user, password, database. Set via env
# vars prefixed CCM_DB_ / COIC_DB_. A source with no host configured is
# simply disabled (its tools return a clean "not configured" error)
# rather than crashing the app — same graceful-degradation pattern as
# the rest of this app's external dependencies.
CCM_DB_HOST = os.environ.get("CCM_DB_HOST", "").strip()
CCM_DB_PORT = int(os.environ.get("CCM_DB_PORT", "5432"))
CCM_DB_USER = os.environ.get("CCM_DB_USER", "").strip()
CCM_DB_PASSWORD = os.environ.get("CCM_DB_PASSWORD", "")
CCM_DB_NAME = os.environ.get("CCM_DB_NAME", "").strip()

COIC_DB_HOST = os.environ.get("COIC_DB_HOST", "").strip()
COIC_DB_PORT = int(os.environ.get("COIC_DB_PORT", "5432"))
COIC_DB_USER = os.environ.get("COIC_DB_USER", "").strip()
COIC_DB_PASSWORD = os.environ.get("COIC_DB_PASSWORD", "")
COIC_DB_NAME = os.environ.get("COIC_DB_NAME", "").strip()

# Every query against either source MUST be schema-qualified with
# `copystorm.` — enforced in db_client.py by always building table
# references as f"{PG_SCHEMA}.{table}" rather than raw table names.
PG_SCHEMA = os.environ.get("PG_SCHEMA", "copystorm").strip()

# Optional: corporate SSL inspection proxies sometimes require a custom CA
# bundle even for direct Postgres (SSL) connections. Leave unset for a
# normal network.
PG_SSL_CA_BUNDLE = os.environ.get("PG_SSL_CA_BUNDLE", "").strip()
PG_SSL_MODE = os.environ.get("PG_SSL_MODE", "prefer").strip()  # asyncpg ssl mode


def summary():
    """For startup logging. Deliberately omits passwords."""
    return {
        "nr_account_id": NEW_RELIC_ACCOUNT_ID,
        "nr_endpoint": NEW_RELIC_GRAPHQL_ENDPOINT,
        "nr_mulesoft_service_like": NEW_RELIC_MULESOFT_SERVICE_LIKE,
        "nr_lambda_arn_like": NEW_RELIC_LAMBDA_ARN_LIKE,
        "nr_extra_exact_services": NEW_RELIC_EXTRA_EXACT_SERVICES,
        "nr_step1_default_since": NEW_RELIC_STEP1_DEFAULT_SINCE,
        "nr_step2_default_since": NEW_RELIC_STEP2_DEFAULT_SINCE,
        "azure_endpoint": AZURE_OPENAI_ENDPOINT,
        "azure_deployment": AZURE_OPENAI_DEPLOYMENT,
        "azure_api_version": AZURE_OPENAI_API_VERSION,
        "kb_pg_host": KB_PG_HOST,
        "kb_pg_db": KB_PG_DB,
        "kb_pg_tables": KB_PG_TABLES,
        "kb_embedding_deployment": AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
        "kb_db_configured": bool(KB_PG_HOST and KB_PG_USER and KB_PG_DB),
        "ccm_db_configured": bool(CCM_DB_HOST and CCM_DB_USER and CCM_DB_NAME),
        "coic_db_configured": bool(COIC_DB_HOST and COIC_DB_USER and COIC_DB_NAME),
    }

