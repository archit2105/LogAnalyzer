"""
nr_client.py — New Relic integration (production log source).

Logs now come from two architectural components across all log types
(no more logtype restriction, no more synthetic-log schema):

  1. Mulesoft applications — service name starts with "phm-XX-prod".
     Unique tracking field: `event`.
  2. AWS Lambda functions   — identified by `faas.arn` containing the
     platform's AWS account id. Unique tracking field: `correlationId`.

Because Cart Order IDs cannot be extracted directly from failure logs, a
two-step query/correlation workflow is used:

  Step 1 — find failures: search for the literal string "Exception Message"
           across all columns, scoped to the platform's services, to pull
           out the tracking id (`event` or `correlationId`) of each failure.

  Step 2 — fetch the full record: look up that tracking id (or, for a
           direct cart lookup, the Cart Order ID itself via
           `data.CARTOrderId`) to retrieve the complete milestone/error
           payload, keyed by the `subject` field (the functional milestone).

Reference:
- NerdGraph docs: https://docs.newrelic.com/docs/apis/nerdgraph/
- NRQL syntax: https://docs.newrelic.com/docs/nrql/get-started/introduction-nrql-new-relics-query-language/
"""

import os
import re
import json
import time
import asyncio
import logging
from typing import Optional

import httpx

import config

logger = logging.getLogger("nr_client")


# --------------------------------------------------------------------------
# Async HTTP client — module-level, reused across NerdGraph calls. Built
# once at first use; closed at shutdown via close_http_client().
# --------------------------------------------------------------------------
_HTTP: Optional[httpx.AsyncClient] = None


def _get_http_client() -> httpx.AsyncClient:
    """Lazily build the shared async HTTP client."""
    global _HTTP
    if _HTTP is None:
        # SSL verification: corporate networks often have an inspection
        # proxy (Zscaler, Netskope, BlueCoat) that intercepts HTTPS using
        # its own certificate. Python's httpx doesn't know about that cert
        # by default, so verification fails with
        # "CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate".
        #
        # Three ways to deal with it, in order of "right":
        #   1. NR_SSL_CA_BUNDLE = path to corporate root CA (.pem) — proper
        #      fix. IT can provide the CA bundle.
        #   2. NR_SSL_VERIFY = "false" — disables verification. Fine for
        #      a corporate-laptop POC where the proxy already inspects
        #      everything anyway; do NOT use in production.
        #   3. Default (no env var set) — strict verification, what
        #      production should use once the CA bundle is in place.
        ca_bundle = os.environ.get("NR_SSL_CA_BUNDLE", "").strip()
        verify_env = os.environ.get("NR_SSL_VERIFY", "").strip().lower()

        if ca_bundle:
            verify: object = ca_bundle
            logger.info("Using corporate CA bundle for New Relic: %s", ca_bundle)
        elif verify_env in ("false", "0", "no", "off"):
            verify = False
            logger.warning(
                "NR_SSL_VERIFY is disabled. Traffic to New Relic is unverified. "
                "Acceptable for POC behind a corporate SSL inspection proxy; "
                "DO NOT use in production. Set NR_SSL_CA_BUNDLE to the "
                "corporate root CA .pem file for the proper fix."
            )
        else:
            verify = True

        _HTTP = httpx.AsyncClient(
            timeout=30.0,
            http2=True,
            verify=verify,
            headers={
                "Content-Type": "application/json",
                "API-Key": config.NEW_RELIC_API_KEY or "",
            },
        )
    return _HTTP


async def close_http_client() -> None:
    """Release the async HTTP client at shutdown."""
    global _HTTP
    if _HTTP is not None:
        await _HTTP.aclose()
        _HTTP = None


# --------------------------------------------------------------------------
# NRQL injection prevention
#
# Two flavors of caller-influenced value show up in these queries:
#   - Identifiers (tracking ids extracted from log data, Cart Order IDs,
#     the SINCE clause) — restricted to a strict whitelist.
#   - Free text (the `subject` / milestone name, e.g. "Apheresis Collection
#     Complete") — allowed to contain spaces, but quotes/backslashes are
#     escaped so it can't break out of the NRQL string literal.
# --------------------------------------------------------------------------
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9\-_.:]+$")
_SINCE_RE = re.compile(
    r"^\d+\s+(minute|minutes|hour|hours|day|days|week|weeks|month|months)\s+ago$",
    re.IGNORECASE,
)


def safe_identifier(value: str) -> str:
    """Validate a value destined for an unquoted-or-simple-quoted NRQL
    fragment (tracking ids, Cart Order IDs). Raises ValueError on anything
    outside the whitelist so unsafe content never reaches NerdGraph."""
    s = str(value)
    if not _IDENTIFIER_RE.match(s):
        raise ValueError(f"Unsafe character in identifier value: {s!r}")
    return s


def safe_literal(value: str) -> str:
    """Escape a free-text value (e.g. a `subject` / milestone name) for use
    inside a single-quoted NRQL string literal."""
    s = str(value)
    return s.replace("\\", "\\\\").replace("'", "\\'")


def safe_since(value: Optional[str], default: str) -> str:
    """Validate a NRQL SINCE clause value against the standard
    '<n> <unit> ago' grammar. Falls back to `default` if not provided,
    rejects anything malformed rather than passing it through."""
    if not value:
        return default
    s = str(value).strip()
    if s.lower() in ("today", "yesterday"):
        return s
    if not _SINCE_RE.match(s):
        raise ValueError(
            f"Unsafe or unrecognized SINCE value: {value!r}. "
            f"Expected a form like '10 days ago' or '24 hours ago'."
        )
    return s


# --------------------------------------------------------------------------
# NRQL builders — the two-step workflow
# --------------------------------------------------------------------------
def _service_filter() -> str:
    """The fixed OR-clause that scopes a query to the platform's Mulesoft
    and AWS Lambda services. Built from config, not user input."""
    parts = []
    for svc in config.NEW_RELIC_EXTRA_EXACT_SERVICES:
        parts.append(f"service = '{safe_literal(svc)}'")
    parts.append(f"service LIKE '{safe_literal(config.NEW_RELIC_MULESOFT_SERVICE_LIKE)}'")
    parts.append(f"faas.arn LIKE '{safe_literal(config.NEW_RELIC_LAMBDA_ARN_LIKE)}'")
    return "(" + " OR ".join(parts) + ")"


def build_step1_nrql(since: Optional[str] = None) -> str:
    """
    Step 1 — identify failures and expose their tracking ids.

    Fixed filters (per production spec): literal search for
    "Exception Message" across all columns, scoped to the platform's
    services. Only the SINCE window is a variable parameter.
    """
    since_clause = safe_since(since, config.NEW_RELIC_STEP1_DEFAULT_SINCE)
    return (
        "SELECT * FROM Log WHERE "
        "(allColumnSearch('Exception Message', insensitive: true)) "
        f"AND {_service_filter()} "
        f"SINCE {since_clause} LIMIT MAX"
    )


def build_step2_nrql(
    tracking_ids: Optional[list] = None,
    cart_order_id: Optional[str] = None,
    milestone: Optional[str] = None,
    since: Optional[str] = None,
) -> str:
    """
    Step 2 — fetch the full record (error payload + milestone details).

    Exactly one of `tracking_ids` or `cart_order_id` should be given:
      - tracking_ids: one or more `event` (Mulesoft) / `correlationId`
        (AWS Lambda) values extracted from Step 1. A single id uses the
        `LIKE '%id%'` form from the spec; multiple ids use `IN (...)` as
        the optimized batch form of the same query.
      - cart_order_id: used directly for Q2-style lookups ("full payload
        for a specific Cart Order ID"), substituted in place of the
        tracking-id filter per the spec's Implementation Strategy.

    `milestone` (optional) narrows to a specific functional milestone via
    the `subject` field.
    """
    if not tracking_ids and not cart_order_id:
        raise ValueError("build_step2_nrql requires tracking_ids or cart_order_id")
    if tracking_ids and cart_order_id:
        raise ValueError("build_step2_nrql accepts only one of tracking_ids or cart_order_id")

    where = [
        "(service LIKE '" + safe_literal(config.NEW_RELIC_MULESOFT_SERVICE_LIKE) + "' "
        "OR (faas.arn LIKE '" + safe_literal(config.NEW_RELIC_LAMBDA_ARN_LIKE) + "' "
        "AND messageObject IS NOT NULL AND message IS NULL))"
    ]

    if cart_order_id:
        cid = safe_identifier(cart_order_id)
        where.append(f"`data.CARTOrderId` LIKE '%{cid}%'")
    else:
        ids = [safe_identifier(t) for t in tracking_ids]
        if len(ids) == 1:
            where.append(f"(event LIKE '%{ids[0]}%' OR correlationId LIKE '%{ids[0]}%')")
        else:
            in_list = ", ".join(f"'{i}'" for i in ids)
            where.append(f"(event IN ({in_list}) OR correlationId IN ({in_list}))")

    if milestone:
        where.append(f"subject = '{safe_literal(milestone)}'")
    where.append("subject IS NOT NULL")

    since_clause = safe_since(since, config.NEW_RELIC_STEP2_DEFAULT_SINCE)
    return (
        "SELECT * FROM Log WHERE " + " AND ".join(where) +
        f" SINCE {since_clause} LIMIT MAX"
    )


# --------------------------------------------------------------------------
# NerdGraph execution
# --------------------------------------------------------------------------
NERDGRAPH_QUERY_TEMPLATE = """
{
  actor {
    account(id: %d) {
      nrql(query: "%s") {
        results
      }
    }
  }
}
"""


def _escape_nrql_for_graphql(nrql):
    """The NRQL string is embedded inside a GraphQL string literal, so we
    must escape backslashes and double-quotes."""
    return nrql.replace("\\", "\\\\").replace('"', '\\"')


async def execute_nrql(nrql):
    """
    Run an NRQL query against NerdGraph. Returns a list of result dicts
    (the contents of `actor.account.nrql.results`).

    Raises RuntimeError on transport failure or NerdGraph-reported errors.
    """
    if not config.NEW_RELIC_ACCOUNT_ID:
        raise RuntimeError(
            "NEW_RELIC_ACCOUNT_ID is not configured. Set it via env var for "
            "this environment before running log queries."
        )
    account_id = int(config.NEW_RELIC_ACCOUNT_ID)
    body = {
        "query": NERDGRAPH_QUERY_TEMPLATE % (account_id, _escape_nrql_for_graphql(nrql))
    }

    client = _get_http_client()
    try:
        resp = await client.post(
            config.NEW_RELIC_GRAPHQL_ENDPOINT,
            content=json.dumps(body).encode("utf-8"),
        )
    except httpx.TimeoutException:
        raise RuntimeError("NerdGraph timed out after 30s")
    except httpx.HTTPError as e:
        raise RuntimeError(f"NerdGraph network error: {e}")

    if not resp.is_success:
        body_err = resp.text[:500] if resp.text else ""
        raise RuntimeError(
            f"NerdGraph HTTP {resp.status_code}: {resp.reason_phrase}. Body: {body_err}"
        )

    try:
        payload = resp.json()
    except ValueError:
        raise RuntimeError(f"NerdGraph returned non-JSON: {resp.text[:500]}")

    if "errors" in payload and payload["errors"]:
        msg = "; ".join(err.get("message", str(err)) for err in payload["errors"])
        raise RuntimeError(f"NerdGraph errors: {msg}")
    try:
        return payload["data"]["actor"]["account"]["nrql"]["results"] or []
    except (KeyError, TypeError):
        raise RuntimeError(f"Unexpected NerdGraph response shape: {resp.text[:500]}")


# --------------------------------------------------------------------------
# Cache — small in-memory TTL cache keyed by the exact NRQL string
#
# Using asyncio.Lock (not threading.Lock) because we're in an async
# context. The lock prevents two concurrent requests for the same key
# from both blowing past a stale entry and double-calling NerdGraph.
# --------------------------------------------------------------------------
_CACHE = {}  # nrql -> (timestamp, results)
_CACHE_LOCK = asyncio.Lock()


async def cached_execute(nrql: str):
    """Execute an NRQL string with caching. Returns (results, cache_hit)."""
    now = time.time()
    async with _CACHE_LOCK:
        cached = _CACHE.get(nrql)
        if cached and (now - cached[0]) < config.NRQL_CACHE_TTL_SECONDS:
            return cached[1], True

    results = await execute_nrql(nrql)
    async with _CACHE_LOCK:
        _CACHE[nrql] = (now, results)
    return results, False


# --------------------------------------------------------------------------
# Record helpers — pull the fields we care about out of a raw NerdGraph
# record. New Relic flattens nested JSON with dots on ingestion (e.g. the
# top-level `data.CARTOrderId` key), while `messageObject` is stored/
# returned as a nested structured attribute.
# --------------------------------------------------------------------------
def get_cart_order_id(record: dict):
    """Find the Cart Order ID on a record, checking multiple possible locations:
    1. Top-level flattened `data.CARTOrderId` (Mulesoft milestone records)
    2. Nested `messageObject.data.CARTOrderId` (same, alternate structure)
    3. Flattened `realTimeIntegration.businessId` (AWS Lambda ATSM logs)
    4. Embedded JSON in `message` field as last resort
    """
    # Check 1: flattened top-level data.CARTOrderId
    if record.get("data.CARTOrderId"):
        return record["data.CARTOrderId"]
    
    # Check 2: nested messageObject.data.CARTOrderId
    mo = record.get("messageObject")
    if isinstance(mo, str):
        try:
            mo = json.loads(mo)
        except (ValueError, TypeError):
            mo = None
    if isinstance(mo, dict):
        data = mo.get("data")
        if isinstance(data, dict) and data.get("CARTOrderId"):
            return data["CARTOrderId"]
    
    # Check 3: realTimeIntegration.businessId (AWS Lambda ATSM logs)
    if record.get("realTimeIntegration.businessId"):
        return record["realTimeIntegration.businessId"]
    
    # Check 4: parse embedded JSON in message field (ATSM exception logs)
    # The message field contains a JSON string with the businessId nested inside
    message = record.get("message")
    if isinstance(message, str):
        try:
            msg_obj = json.loads(message)
            if isinstance(msg_obj, dict):
                # Try realTimeIntegrationMessageObject.businessId
                rtim = msg_obj.get("realTimeIntegrationMessageObject")
                if isinstance(rtim, dict) and rtim.get("businessId"):
                    return rtim["businessId"]
                # Try direct businessId
                if msg_obj.get("businessId"):
                    return msg_obj["businessId"]
        except (ValueError, TypeError):
            pass
    
    return None


def get_milestone(record: dict):
    """The `subject` field explicitly defines the functional milestone."""
    return record.get("subject")


def get_tracking_id(record: dict):
    """Return (field_name, value) for whichever tracking field is present
    — `event` for Mulesoft-origin logs, `correlationId` for AWS Lambda."""
    if record.get("event"):
        return "event", record["event"]
    if record.get("correlationId"):
        return "correlationId", record["correlationId"]
    return None, None


def get_exception_message(record: dict):
    """
    Step-1 records are matched because *some* field contains the literal
    text "Exception Message" (via allColumnSearch) — the spec doesn't fix
    which field, so scan the record's string values and return the first
    one that contains it, along with the field name it was found in.
    """
    for key, value in record.items():
        if isinstance(value, str) and "exception message" in value.lower():
            return key, value
    return None, None


def get_timestamp(record: dict):
    """Best-effort sortable timestamp: prefer the numeric `timestamp` (ms
    since epoch), fall back to the ISO `time` string."""
    return record.get("timestamp") or record.get("time") or ""


# --------------------------------------------------------------------------
# Health check — used at startup to verify creds and connectivity work
# --------------------------------------------------------------------------
async def healthcheck():
    """
    Run a minimal Step-1-shaped NRQL against the configured account to
    verify that:
    - API key is valid
    - account ID is reachable
    - the service filters match at least one record recently

    Returns a tuple (ok: bool, message: str, sample_record: dict|None).
    """
    nrql = (
        f"SELECT count(*) FROM Log WHERE {_service_filter()} SINCE 1 day ago"
    )
    try:
        results = await execute_nrql(nrql)
    except Exception as e:
        return False, f"NerdGraph call failed: {e}", None

    count = results[0].get("count", 0) if results else 0
    if count == 0:
        return False, (
            "Query succeeded but found no logs matching the Mulesoft/Lambda "
            "service filters in the last day. This may just mean it's a "
            "quiet period — check the New Relic UI directly if this "
            "persists."
        ), None

    sample_nrql = (
        f"SELECT * FROM Log WHERE {_service_filter()} SINCE 1 day ago LIMIT 1"
    )
    try:
        sample = await execute_nrql(sample_nrql)
        sample_record = sample[0] if sample else None
    except Exception:
        sample_record = None

    return True, f"Healthy: {count} matching log(s) found in the last day.", sample_record
