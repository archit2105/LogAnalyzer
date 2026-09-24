"""
embeddings.py — Azure OpenAI embeddings client (text-embedding-3-large).

Used by kb_retrieval.py to embed a user's query before running a
pgvector similarity search. Must produce vectors in the SAME embedding
space the Confluence ingestion job used to embed the stored chunks, or
distances are meaningless — hence the shared deployment name/config.

Adapted from the ingestion team's embeddings.py: the only real change is
reading the endpoint/deployment/key from config.py (env vars) instead of
a hardcoded (and, in the original file, blank) endpoint string.
"""
import requests

import config

EMBEDDING_BATCH_SIZE = 100


def _endpoint_url() -> str:
    """
    Azure OpenAI embeddings REST URL, e.g.:
    https://YOUR-RESOURCE.openai.azure.com/openai/deployments/text-embedding-3-large/embeddings?api-version=2024-02-01

    If AZURE_OPENAI_EMBEDDING_ENDPOINT is already a full URL (contains
    "/openai/deployments/"), it's used as-is. Otherwise it's treated as
    just the resource base (e.g. "https://YOUR-RESOURCE.openai.azure.com")
    and the deployment path + api-version are appended automatically.
    """
    endpoint = config.AZURE_OPENAI_EMBEDDING_ENDPOINT
    if not endpoint:
        raise RuntimeError(
            "AZURE_OPENAI_EMBEDDING_ENDPOINT is not set. Set it to either "
            "the full embeddings URL, or just your Azure OpenAI resource "
            "base (e.g. 'https://YOUR-RESOURCE.openai.azure.com')."
        )

    if "/openai/deployments/" in endpoint:
        return endpoint

    base = endpoint.rstrip("/")
    deployment = config.AZURE_OPENAI_EMBEDDING_DEPLOYMENT
    api_version = config.AZURE_OPENAI_API_VERSION or "2024-02-01"
    return f"{base}/openai/deployments/{deployment}/embeddings?api-version={api_version}"


def get_embeddings(texts, api_key=None, batch_size=EMBEDDING_BATCH_SIZE):
    """
    Embed a list of texts, batching requests at `batch_size`. Returns a
    list of embedding vectors (list[float]) in the same order as `texts`.
    """
    api_key = api_key or config.AZURE_OPENAI_EMBEDDING_API_KEY
    if not api_key:
        raise RuntimeError(
            "No Azure OpenAI embedding API key available. Set "
            "AZURE_OPENAI_EMBEDDING_API_KEY (or AZURE_OPENAI_API_KEY as a "
            "fallback)."
        )

    url = _endpoint_url()
    headers = {
        "Content-Type": "application/json",
        "api-key": api_key,
    }

    embeddings = []

    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]

        response = requests.post(
            url=url,
            headers=headers,
            json={"input": batch},
            timeout=30,
        )
        response.raise_for_status()

        data = sorted(
            response.json()["data"],
            key=lambda item: item["index"]
        )
        embeddings.extend(item["embedding"] for item in data)

    return embeddings


def get_embedding(text, api_key=None):
    """Embed a single piece of text. Convenience wrapper over get_embeddings()."""
    return get_embeddings([text], api_key=api_key)[0]
