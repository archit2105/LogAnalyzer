"""
tools.py — implementations of the tools the LLM can call:

1. find_failed_cart_orders(...)  — Q1: how many Cart Order IDs failed in the
                                    last X days, and which ones. Runs the
                                    two-step New Relic query/correlation
                                    workflow (find failures -> correlate to
                                    Cart Order ID).
2. get_cart_order_payload(...)   — Q2: full payload + milestone history for
                                    a specific Cart Order ID (optionally a
                                    specific milestone), enriched with any
                                    associated Exception Message.
3. search_confluence_kb(...)      — semantic search over Confluence pages
                                    chunked/embedded (Azure OpenAI
                                    text-embedding-3-large) into a
                                    PostgreSQL + pgvector table by a
                                    separate ingestion job. See
                                    pgvector_client.py.
4. query_cart_metadata(...)      — direct lookup against PostgreSQL cart
                                    metadata, split across two data
                                    sources (CCM and COIC), routed by
                                    milestone. A few milestones live in
                                    New Relic instead — this tool routes
                                    those back to get_cart_order_payload.

All functions return JSON-serializable dicts that go straight back to the
LLM.
"""

from collections import defaultdict

import config
import nr_client
import db_client
import kb_store
import pgvector_client


# --------------------------------------------------------------------------
# Module-level state — loaded once at startup
# --------------------------------------------------------------------------
async def initialize():
    """
    Call once at app startup from the FastAPI lifespan handler.

    Marked async because it needs to `await db_client.initialize()` — which
    does network I/O to open the CCM and COIC PostgreSQL connection pools.
    """
    print(
        f"[tools] Log source: New Relic. Account={config.NEW_RELIC_ACCOUNT_ID}, "
        f"endpoint={config.NEW_RELIC_GRAPHQL_ENDPOINT}."
    )

    # The KB store's connection pool is opened lazily on first query (see
    # kb_store.py), but we probe it here so bad credentials/host show up
    # in the startup log instead of silently failing on the first user
    # question.
    if pgvector_client.is_available():
        count = pgvector_client.chunk_count()
        print(
            f"[tools] KB store connected: host={config.KB_PG_HOST}, "
            f"db={config.KB_PG_DB}, tables={config.KB_PG_TABLES}, "
            f"{count if count is not None else '?'} chunk(s)."
        )
    else:
        print(
            f"[tools] WARNING: KB store not reachable at "
            f"host={config.KB_PG_HOST}, db={config.KB_PG_DB}, "
            f"tables={config.KB_PG_TABLES}. "
            f"search_confluence_kb will return an error until this is fixed."
        )

    # Bring up both PostgreSQL connection pools (CCM and COIC). Each is
    # independent — if one source's env vars are unset or unreachable,
    # only lookups routed to that source fail gracefully; the other
    # source and every other tool keep working.
    await db_client.initialize()


# --------------------------------------------------------------------------
# Tool: find_failed_cart_orders  (Q1)
# --------------------------------------------------------------------------
async def find_failed_cart_orders(time_window=None):
    """
    Q1: How many Cart Order IDs failed in the last X days, and which ones?

    Two-step workflow:
      Step 1 — search all Mulesoft (phm-XX-prod) and AWS Lambda logs for the
               literal string "Exception Message" within `time_window`
               (default: 10 days). Each hit yields a tracking id (`event`
               for Mulesoft, `correlationId` for AWS Lambda).
      Step 2 — batch-correlate all distinct tracking ids in a single
               optimized query (`event`/`correlationId` IN (...)) against
               the last 90 days to pull each record's `data.CARTOrderId`.

    Returns a dict with the total failure-log count, the distinct list of
    failed Cart Order IDs, and per-cart failure context (tracking id,
    service/faas.arn origin, exception message, timestamp) so the caller
    can enrich its answer without a second round trip.
    """
    step1_nrql = nr_client.build_step1_nrql(since=time_window)
    try:
        step1_results, step1_cache_hit = await nr_client.cached_execute(step1_nrql)
    except Exception as e:
        return {"error": f"New Relic Step 1 (find failures) query failed: {e}"}

    if not step1_results:
        return {
            "time_window": time_window or config.NEW_RELIC_STEP1_DEFAULT_SINCE,
            "total_failure_logs": 0,
            "failed_cart_order_count": 0,
            "failed_cart_order_ids": [],
            "failures": [],
            "step1_nrql": step1_nrql,
        }

    # Collect distinct tracking ids, remembering which failure record(s)
    # each one came from so we can re-attach exception context after Step 2.
    # ALSO: collect any cart IDs we can extract directly from Step 1 records
    # (some logs like ATSM have the cart ID right there, no Step 2 correlation needed).
    tracking_to_failures = defaultdict(list)
    direct_cart_ids = {}  # cart_id -> first Step 1 record that has it
    
    for rec in step1_results:
        field, tid = nr_client.get_tracking_id(rec)
        exc_field, exc_msg = nr_client.get_exception_message(rec)
        
        # If we have a tracking ID, remember the failure context
        if tid:
            tracking_to_failures[tid].append({
                "tracking_field": field,
                "service": rec.get("service"),
                "faas_arn": rec.get("faas.arn"),
                "exception_message": exc_msg,
                "timestamp": nr_client.get_timestamp(rec),
            })
        
        # ALSO: try to extract a cart ID directly (for logs that have it)
        cart_id = nr_client.get_cart_order_id(rec)
        if cart_id and cart_id not in direct_cart_ids:
            direct_cart_ids[cart_id] = {
                "cart_order_id": cart_id,
                "milestone": nr_client.get_milestone(rec),
                "tracking_id": tid,
                "service": rec.get("service"),
                "faas_arn": rec.get("faas.arn"),
                "exception_message": exc_msg,
                "timestamp": nr_client.get_timestamp(rec),
            }

    distinct_ids = list(tracking_to_failures.keys())
    
    # Start with cart IDs extracted directly from Step 1 (these are already populated)
    by_cart_id = dict(direct_cart_ids)
    
    # If we have tracking IDs, run Step 2 to find milestone records and correlate
    # cart IDs from records that don't have them directly
    if distinct_ids:
        step2_nrql = nr_client.build_step2_nrql(tracking_ids=distinct_ids)
        try:
            step2_results, step2_cache_hit = await nr_client.cached_execute(step2_nrql)
        except Exception as e:
            # Step 2 failure is non-fatal if we already have some cart IDs from Step 1
            if direct_cart_ids:
                step2_results = []
            else:
                return {
                    "error": f"New Relic Step 2 (correlate to Cart Order ID) query failed: {e}",
                    "step1_nrql": step1_nrql,
                }

        # Match each Step 2 record back to the tracking id(s) it satisfies, and
        # build/enrich entries per (cart_order_id) with the richest available context.
        for rec in step2_results:
            cart_id = nr_client.get_cart_order_id(rec)
            if not cart_id:
                continue
            _, tid = nr_client.get_tracking_id(rec)
            failure_ctx_list = tracking_to_failures.get(tid, [])
            failure_ctx = failure_ctx_list[0] if failure_ctx_list else {}
            
            # If we already have this cart_id from Step 1, enrich it; otherwise, add it
            if cart_id in by_cart_id:
                # Enrich with Step 2 context (milestone, etc.)
                entry = by_cart_id[cart_id]
                if not entry.get("milestone"):
                    entry["milestone"] = nr_client.get_milestone(rec)
            else:
                # New cart_id from Step 2
                entry = {
                    "cart_order_id": cart_id,
                    "milestone": nr_client.get_milestone(rec),
                    "tracking_id": tid,
                    "service": failure_ctx.get("service") or rec.get("service"),
                    "exception_message": failure_ctx.get("exception_message"),
                    "timestamp": nr_client.get_timestamp(rec),
                }
                by_cart_id[cart_id] = entry
            
            # Always prefer exception_message if Step 1 has it
            if not entry.get("exception_message") and failure_ctx.get("exception_message"):
                entry["exception_message"] = failure_ctx["exception_message"]

    # If we found no cart IDs at all, return empty but don't error
    if not by_cart_id:
        return {
            "time_window": time_window or config.NEW_RELIC_STEP1_DEFAULT_SINCE,
            "total_failure_logs": len(step1_results),
            "failed_cart_order_count": 0,
            "failed_cart_order_ids": [],
            "failures": [],
            "note": "Exception logs were found but none carried a recognizable Cart Order ID.",
            "step1_nrql": step1_nrql,
        }

    failures = sorted(by_cart_id.values(), key=lambda f: f["cart_order_id"])

    return {
        "time_window": time_window or config.NEW_RELIC_STEP1_DEFAULT_SINCE,
        "total_failure_logs": len(step1_results),
        "distinct_tracking_ids": len(distinct_ids),
        "failed_cart_order_count": len(failures),
        "failed_cart_order_ids": [f["cart_order_id"] for f in failures],
        "failures": failures,
        "step1_nrql": step1_nrql,
        "step2_nrql": step2_nrql,
    }


# --------------------------------------------------------------------------
# Tool: get_cart_order_payload  (Q2)
# --------------------------------------------------------------------------
async def get_cart_order_payload(cart_order_id, milestone=None, time_window=None):
    """
    Q2: Retrieve the full payload for a specific Cart Order ID (and,
    optionally, a specific functional milestone / `subject`).

    Looks up historical execution logs for the cart, defaulting to the last
    90 days. If multiple records match, the LATEST one (by timestamp) is
    selected. Also re-runs a Step-1-style exception search scoped to this
    cart so the exact Exception Message (if any) can be attached alongside
    the payload.
    """
    if not cart_order_id:
        return {"error": "cart_order_id is required."}

    step2_nrql = nr_client.build_step2_nrql(
        cart_order_id=cart_order_id, milestone=milestone, since=time_window,
    )
    try:
        results, cache_hit = await nr_client.cached_execute(step2_nrql)
    except Exception as e:
        return {"error": f"New Relic query failed: {e}", "step2_nrql": step2_nrql}

    if not results:
        return {
            "cart_order_id": cart_order_id,
            "milestone": milestone,
            "found": False,
            "note": "No matching execution logs found for this Cart Order ID"
                    + (f" and milestone '{milestone}'" if milestone else "")
                    + " in the lookup window.",
            "step2_nrql": step2_nrql,
        }

    # Multiple matching logs -> always select the latest by timestamp.
    latest = max(results, key=lambda r: nr_client.get_timestamp(r))

    # Enrichment: search Step-1-style for an Exception Message tied to this
    # same cart so we can surface exact failure context alongside the payload.
    exception_context = None
    try:
        exc_nrql = (
            "SELECT * FROM Log WHERE "
            "(allColumnSearch('Exception Message', insensitive: true)) "
            f"AND (allColumnSearch('{nr_client.safe_literal(cart_order_id)}', insensitive: true)) "
            f"SINCE {nr_client.safe_since(time_window, config.NEW_RELIC_STEP2_DEFAULT_SINCE)} LIMIT MAX"
        )
        exc_results, _ = await nr_client.cached_execute(exc_nrql)
        if exc_results:
            exc_rec = max(exc_results, key=lambda r: nr_client.get_timestamp(r))
            _, exc_msg = nr_client.get_exception_message(exc_rec)
            if exc_msg:
                exception_context = {
                    "exception_message": exc_msg,
                    "timestamp": nr_client.get_timestamp(exc_rec),
                }
    except Exception:
        # Enrichment is best-effort — never fail the whole tool call over it.
        pass

    return {
        "cart_order_id": cart_order_id,
        "milestone": nr_client.get_milestone(latest) or milestone,
        "found": True,
        "matched_count": len(results),
        "payload": latest,
        "exception_context": exception_context,
        "step2_nrql": step2_nrql,
    }


# --------------------------------------------------------------------------
# Tool: search_confluence_kb
# --------------------------------------------------------------------------
def search_confluence_kb(query, top_k=None, filters=None):
    """
    Semantic search over Confluence pages chunked and embedded (Azure
    OpenAI text-embedding-3-large) into PostgreSQL + pgvector by a
    separate ingestion job (see pgvector_client.py for connection
    details).

    Unlike the retired TF-IDF/ChromaDB setup, this uses real semantic
    embeddings — natural, descriptive queries work well here; there's no
    need to strip down to bare keywords.

    NOT limited to a single Confluence source: every table listed in
    config.KB_PG_TABLES (one per Confluence space/ingestion batch, as
    configured) is searched, and the results are merged and re-ranked
    before the top_k are returned. Callers don't need to know or guess
    which table/page holds the answer.

    `filters` (optional): NOT currently supported by the underlying
    pgvector schema — accepted for interface compatibility only and
    ignored if passed (a warning is logged, not an error).
    """
    try:
        results = pgvector_client.search(query, top_k=top_k, where=filters)
    except RuntimeError as e:
        return {"error": str(e)}

    return {
        "query": query,
        "filters_applied": filters or {},
        "result_count": len(results),
        "results": [
            {
                "page_id": r["page_id"],
                "page_title": r["page_title"],
                "chunk_id": r["chunk_id"],
                "section_id": r["section_id"],
                "text": r["chunk"],
                "metadata": r["metadata"],
                "page_version": r["page_version"],
                "distance": r["distance"],
                "matched_by_title": r["title_match"],
                "source_table": r["source_table"],
            }
            for r in results
        ],
    }


# --------------------------------------------------------------------------
# Tool: query_cart_metadata
# --------------------------------------------------------------------------
async def query_cart_metadata(
    mode="lookup", cart_id=None, milestone=None, source=None, fields=None,
):
    """
    Two modes:

    mode='lookup' (default):
        Look up order__c (+ matching milestone rows) for a SPECIFIC cart.
        Requires cart_id. Source (CCM vs COIC) is resolved automatically
        from `milestone` if given; pass `source` explicitly to override.
        If `milestone` resolves to New Relic instead of Postgres, this
        returns a routing note instead of querying anything -- the caller
        should call get_cart_order_payload for that milestone instead.

    mode='list_overdue':
        CCM only. Every cart whose manufacturingenddatetime__c has
        passed but manufacturingstatus__c is still IN-PROGRESS. cart_id
        and milestone are ignored in this mode.

    Args:
        mode: "lookup" or "list_overdue". Default "lookup".
        cart_id: Cart identifier (order__c.name). Required for
            mode="lookup", ignored for mode="list_overdue".
        milestone: Optional milestone name, e.g. "Manufacturing Start".
            Determines CCM vs COIC vs New Relic automatically. Lookup
            mode only.
        source: Optional explicit override -- "ccm" or "coic". Takes
            precedence over the milestone-based routing. Use only when
            you already know which source the data lives in.
        fields: Optional subset of order__c columns to return.

    Returns one of:
        - {"found": True, "source": "ccm"|"coic", "order": {...},
           "milestones": [...]}                       -- matched
        - {"found": False, "note": "..."}              -- no match
        - {"routed": True, "source": "new_relic", "note": "..."} --
          milestone's data lives in New Relic, not Postgres
        - {"error": "..."}                             -- tool unavailable
    """
    if mode == "list_overdue":
        return await db_client.find_overdue_carts(fields=fields)

    if mode == "lookup":
        if not cart_id:
            return {
                "error": "cart_id is required when mode='lookup'.",
                "found": False,
            }
        return await db_client.fetch_cart_metadata(
            cart_id=cart_id, milestone=milestone, source=source, fields=fields,
        )

    return {
        "error": f"Unknown mode: {mode!r}. Use 'lookup' or 'list_overdue'.",
        "found": False,
    }


# --------------------------------------------------------------------------
# Tool schemas for the LLM (used in app.py when constructing the API call)
# --------------------------------------------------------------------------
TOOL_SCHEMAS = [
    {
        "name": "find_failed_cart_orders",
        "description": (
            "Q1 — Find how many Cart Order IDs failed in the last X days, "
            "and which ones. Runs New Relic's two-step failure/correlation "
            "workflow: (1) search all Mulesoft (phm-XX-prod) and AWS "
            "Lambda logs for the literal string 'Exception Message' to "
            "pull out each failure's tracking id (`event` for Mulesoft, "
            "`correlationId` for AWS Lambda); (2) batch-correlate those "
            "tracking ids to their Cart Order IDs (`data.CARTOrderId`).\n\n"
            "Use cases:\n"
            "- 'how many cart orders failed in the last 10 days' -> "
            "find_failed_cart_orders(time_window='10 days ago')\n"
            "- 'which carts failed today' -> find_failed_cart_orders("
            "time_window='24 hours ago')\n"
            "- 'list the failed cart order IDs this week' -> "
            "find_failed_cart_orders(time_window='7 days ago')\n\n"
            "The response includes `failed_cart_order_count`, "
            "`failed_cart_order_ids` (the list), and `failures` (per-cart "
            "detail: milestone, tracking_id, service, exception_message, "
            "timestamp) so you don't need a follow-up call for basic "
            "failure context.\n\n"
            "MILESTONE CONTEXT: each failure's `milestone` can be ANY "
            "functional milestone in the pipeline — this is not limited "
            "to manufacturing start. Whenever the user also wants to "
            "understand a milestone that shows up here (its definition, "
            "expected behavior, dependencies, or documentation), call "
            "search_confluence_kb for that milestone name IN THE SAME "
            "TURN as this tool, so both results are ready together for a "
            "single combined answer rather than a follow-up round trip.\n\n"
            "Cart/order identifiers returned here may be in any format "
            "the source systems use — don't assume or validate against a "
            "fixed prefix/pattern."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "time_window": {
                    "type": "string",
                    "description": (
                        "NRQL SINCE clause, e.g. '10 days ago', '24 hours "
                        "ago', '7 days ago'. Defaults to 10 days ago if "
                        "omitted, per the production spec."
                    )
                }
            }
        }
    },
    {
        "name": "get_cart_order_payload",
        "description": (
            "Q2 — Retrieve the full payload for a SPECIFIC Cart Order ID, "
            "optionally scoped to one functional milestone (the `subject` "
            "field, e.g. 'Apheresis Collection Complete'). Looks up "
            "historical execution logs (default lookback: 90 days); if "
            "multiple records match, the LATEST one by timestamp is "
            "returned. Also attaches any associated Exception Message "
            "found for the same cart, if one exists.\n\n"
            "Use cases:\n"
            "- 'give me the payload for US-000815' -> get_cart_order_payload("
            "cart_order_id='US-000815')\n"
            "- 'what does the Apheresis Collection Complete record look "
            "like for EU-00081' -> get_cart_order_payload(cart_order_id="
            "'EU-00081', milestone='Apheresis Collection Complete')\n"
            "- 'what went wrong with cart US-000815' -> "
            "get_cart_order_payload(cart_order_id='US-000815') and read "
            "`exception_context`.\n\n"
            "Cart Order IDs are dynamic values — they may come in any "
            "format the source systems use (regional prefixes, numeric "
            "ids, GUIDs, etc.). 'US-000815' and 'EU-00081' above are just "
            "illustrative examples, not the only valid formats — never "
            "reject or reformat an id the user gives you. If the user "
            "hasn't given a Cart Order ID for a single-cart question, ask "
            "before calling this tool rather than guessing one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cart_order_id": {
                    "type": "string",
                    "description": (
                        "The Cart Order ID, in whatever format the "
                        "caller provides (e.g. 'US-000815', 'EU-00081', "
                        "or any other identifier format) — pass it "
                        "through as given, don't assume a fixed prefix."
                    )
                },
                "milestone": {
                    "type": "string",
                    "description": (
                        "Optional functional milestone name to narrow the "
                        "lookup, matched against the `subject` field "
                        "exactly, e.g. 'Apheresis Collection Complete'."
                    )
                },
                "time_window": {
                    "type": "string",
                    "description": (
                        "NRQL SINCE clause overriding the default 90-day "
                        "lookback, e.g. '30 days ago'."
                    )
                }
            },
            "required": ["cart_order_id"]
        }
    },
    {
        "name": "search_confluence_kb",
        "description": (
            "Semantic search over Confluence documentation, chunked and "
            "embedded (Azure OpenAI text-embedding-3-large) into "
            "PostgreSQL with pgvector. Use this for ANY question that "
            "isn't about live New Relic log data (find_failed_cart_orders "
            "/ get_cart_order_payload) and isn't about cart metadata in "
            "Postgres (query_cart_metadata) — meaning, business impact, "
            "downstream systems affected, root cause, or how to fix/"
            "remediate something, architecture questions, or anything "
            "else that requires pulling context from documentation "
            "rather than live data.\n\n"
            "Query style: this is real semantic embedding search, not "
            "keyword matching — natural, descriptive questions work "
            "well (e.g. 'what happens when FP Labeling fails'), no need "
            "to strip down to bare keywords.\n\n"
            "The answer is often spread across multiple Confluence pages "
            "rather than stated in one place — call this tool more than "
            "once with refined/follow-up queries when the first call "
            "surfaces a system, term, or reference that needs its own "
            "lookup (e.g. first query the error itself, then query the "
            "downstream system it names, then query that system's "
            "remediation runbook) rather than settling for one shot.\n\n"
            "Use cases:\n"
            "- 'what does <error text> mean' -> search_confluence_kb(query='what does <error text> mean')\n"
            "- 'business impact of <error/system>' -> search_confluence_kb(query='business impact of <error/system>')\n"
            "- 'what downstream systems are affected by <error>' -> search_confluence_kb(query='downstream systems affected by <error>')\n"
            "- 'how do I fix <error>' -> search_confluence_kb(query='how to fix <error>')\n"
            "- 'explain the architecture' -> search_confluence_kb(query='platform architecture overview')"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language query describing what you want to know."
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of top chunks to return. Defaults to a server-side setting (currently 8)."
                },
                "filters": {
                    "type": "object",
                    "description": (
                        "Reserved for future use — the current pgvector "
                        "schema doesn't support metadata filtering. Omit "
                        "this parameter."
                    )
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "query_cart_metadata",
        "description": (
            "Look up STATIC REFERENCE DATA about carts directly from "
            "PostgreSQL (order__c + milestone tables). There are TWO "
            "separate PostgreSQL data sources -- CCM (a.k.a. COM NA) and "
            "COIC -- and this tool automatically routes to the right one "
            "based on the milestone you pass. A few milestones' metadata "
            "lives in New Relic logs instead of Postgres; for those, this "
            "tool returns a routing note telling you to call "
            "get_cart_order_payload instead (extract metadata only, not "
            "the full log -- see that tool's own response-handling rules).\n\n"
            "MODE 'lookup' (default): get metadata for a SPECIFIC cart. "
            "Pass cart_id (required) and, whenever the question names a "
            "milestone, pass milestone too -- this determines CCM vs COIC "
            "automatically and also returns that milestone's row from the "
            "milestone table (actual date, planned date). Omit milestone "
            "for cart-level fields only (defaults to CCM's order__c).\n\n"
            "MODE 'list_overdue': CCM only. Every cart whose "
            "manufacturingenddatetime__c has passed but whose "
            "manufacturingstatus__c is still 'IN-PROGRESS'. cart_id and "
            "milestone are not used in this mode.\n\n"
            "CCM order__c fields: apheresispostatus__c, "
            "manufacturingstatus__c, orderstatus_c, "
            "manufacturingenddatetime__c, manufacturingstartdatetime__c "
            "(acts as actual manufacturing start), "
            "plannedmanufacturingstartdate__c (planned manufacturing "
            "start -- compare to today's date for due-date questions). "
            "NOTE: ordercancelled__c does NOT exist in CCM -- COIC only.\n"
            "CCM milestone table (ordermilestones__c) fields: name, "
            "milestonedatetime__c. Joined via this table's order__c "
            "column (NOT its own id) matching order__c.id.\n\n"
            "COIC order__c fields: ordercancelled__c (COIC's order__c is "
            "mainly a join anchor -- most COIC detail lives in the "
            "milestone table).\n"
            "COIC milestone table (ordermilestone__c) fields: "
            "milestonename__c, actaldate__c (actual date the event "
            "happened -- if absent, the event hasn't occurred yet), "
            "planneddate__c.\n\n"
            "Milestone -> source routing (pass the exact milestone name "
            "as one of these to get automatic routing):\n"
            "  CCM: FDP Batch/Lot ID, Apheresis Collection Complete, "
            "Cryopreservation Process End, Manufacturing Start, "
            "Manufacturing End, COI/COC Order Created, FP Labeling, "
            "Order Approved, Cryopreserved Apheresis Received at "
            "Manufacturing Site, Cryopreserved Apheresis QA Released at "
            "Manufacturing Site\n"
            "  COIC: Receipt at Cryopreservation Site, Receipt at "
            "Manufacturing Site, FP QA Release at Manufacturing Site, FP "
            "Drop Off at Infusion Site, FP Receipt at Infusion Site, "
            "Shipment Preparation at Collection Site, Shipment "
            "Preparation at Manufacturing Site, Fresh Apheresis PickedUp "
            "from Treatment Center, Shipment Preparation at "
            "Cryopreservation Site, FP Pick Up from Manufacturing Site, "
            "Fresh Apheresis DroppedOff at CryoSite, Cryopreserved "
            "Apheresis Pick Up, Cryopreserved Apheresis Drop Off at "
            "Manufacturing Site\n"
            "  New Relic (NOT Postgres -- routes to get_cart_order_payload): "
            "Purchase Order for CMO, Purchase Order Acknowledgement, "
            "Advanced Shipment Notice, Goods Receipt Retain bag, Goods "
            "Receipt (Cryo Aph), Goods Receipt Finished Product, Finished "
            "Goods Batch Master, Patient Enrollment Complete\n\n"
            "PII: never request or surface personal/PII columns through "
            "this tool -- only the business fields listed above exist in "
            "its allowlist; anything else is silently dropped.\n\n"
            "When to use this tool:\n"
            "  Lookup mode:\n"
            "  - 'is cart X cancelled' / 'manufacturing status for cart X'\n"
            "  - 'planned vs actual manufacturing start for cart X'\n"
            "  - 'has cart X hit FP Labeling yet' (pass milestone='FP Labeling')\n"
            "  - 'when is the planned date for <milestone> on cart X'\n"
            "  List-overdue mode:\n"
            "  - 'which carts are overdue' / 'delayed manufacturing runs'\n\n"
            "When NOT to use this tool:\n"
            "  - Live failure/error investigation -> use "
            "find_failed_cart_orders / get_cart_order_payload\n"
            "  - Definitions, error meanings, architecture, downstream "
            "impact -> use search_confluence_kb\n\n"
            "If a question needs both metadata AND live logs, call BOTH "
            "tools in the same turn.\n\n"
            "How to read the response:\n"
            "  - found=false: nothing matched. State that plainly, don't "
            "invent values.\n"
            "  - routed=true, source='new_relic': call "
            "get_cart_order_payload instead, as instructed in the note.\n"
            "  - found=true: use `order` (cart-level fields) and, if "
            "present, `milestones` (rows from the milestone table) to "
            "answer. An empty `milestones` list for a named milestone "
            "means that event hasn't occurred yet -- say so, don't guess "
            "a date."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["lookup", "list_overdue"],
                    "description": (
                        "Which query to run. 'lookup' (default) for "
                        "questions about a specific cart_id. "
                        "'list_overdue' for the overdue-manufacturing "
                        "question (CCM only)."
                    )
                },
                "cart_id": {
                    "type": "string",
                    "description": (
                        "Cart identifier (order__c.name), in whatever "
                        "format it's given (e.g. 'US-000815', 'EU-00081', "
                        "or any other format) -- don't assume a fixed "
                        "prefix. Required when mode='lookup', ignored "
                        "otherwise."
                    )
                },
                "milestone": {
                    "type": "string",
                    "description": (
                        "Exact milestone name, e.g. 'Manufacturing Start' "
                        "or 'FP QA Release at Manufacturing Site'. "
                        "Determines whether CCM or COIC Postgres is "
                        "queried (or routes to New Relic instead, for "
                        "milestones not stored in Postgres). Omit for "
                        "cart-level-only questions (defaults to CCM)."
                    )
                },
                "source": {
                    "type": "string",
                    "enum": ["ccm", "coic"],
                    "description": (
                        "Optional explicit override for which Postgres "
                        "source to query, taking precedence over "
                        "milestone-based routing. Only set this if you "
                        "already know which source the data is in."
                    )
                },
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "apheresispostatus__c",
                            "manufacturingstatus__c",
                            "orderstatus_c",
                            "manufacturingenddatetime__c",
                            "manufacturingstartdatetime__c",
                            "plannedmanufacturingstartdate__c",
                            "ordercancelled__c",
                            "id",
                            "name",
                        ]
                    },
                    "description": (
                        "Optional list of specific order__c columns to "
                        "return. Default: all allowed columns for the "
                        "resolved source. id and name are always "
                        "included so the row is self-identifying. Note: "
                        "ordercancelled__c only exists in COIC -- it's "
                        "silently dropped if requested against CCM. Note: "
                        "passing fields skips the milestone-table join "
                        "unless `milestone` is also given."
                    )
                }
            },
            "required": []
        }
    }
]


# --------------------------------------------------------------------------
# OpenAI-format tool schemas.
#
# The OpenAI Chat Completions API expects tools in a different shape than
# Anthropic's Messages API:
#
#   Anthropic: {name, description, input_schema}
#   OpenAI:    {type: "function", function: {name, description, parameters}}
#
# We keep TOOL_SCHEMAS above as the single source of truth (it's the shape
# we originally designed against) and translate here. Any change to a
# tool's schema propagates automatically to the OpenAI format.
# --------------------------------------------------------------------------
TOOL_SCHEMAS_OPENAI = [
    {
        "type": "function",
        "function": {
            "name": schema["name"],
            "description": schema["description"],
            "parameters": schema["input_schema"],
        },
    }
    for schema in TOOL_SCHEMAS
]


async def dispatch_async(tool_name, tool_input):
    """
    Called by app.py inside the async agent loop. Routes by tool name,
    awaiting tools that do I/O and calling the in-memory tool synchronously.
    """
    if tool_name == "find_failed_cart_orders":
        return await find_failed_cart_orders(**tool_input)
    if tool_name == "get_cart_order_payload":
        return await get_cart_order_payload(**tool_input)
    if tool_name == "search_confluence_kb":
        # kb_store.py uses psycopg2 (blocking, not asyncio-native) under
        # the hood, so this call blocks the event loop for its duration —
        # acceptable for a POC/moderate-traffic app; if this becomes a
        # bottleneck, swap kb_store.py to psycopg3's async API or run it
        # via asyncio.to_thread().
        return search_confluence_kb(**tool_input)
    if tool_name == "query_cart_metadata":
        return await query_cart_metadata(**tool_input)
    return {"error": f"Unknown tool: {tool_name}"}


# Sync wrapper kept for backward compat with any code that still calls the
# old name (and for ad-hoc test scripts). Avoid using in the request path.
def dispatch(tool_name, tool_input):
    """Synchronous dispatch — runs the async path on a fresh event loop."""
    import asyncio
    return asyncio.run(dispatch_async(tool_name, tool_input))


def kb_available() -> bool:
    """True if the PostgreSQL/pgvector KB store is reachable."""
    return pgvector_client.is_available()


async def shutdown() -> None:
    """Release async resources held by tool dependencies."""
    await db_client.close_pools()
    await nr_client.close_http_client()
    kb_store.close_pool()  # psycopg2 pool close is synchronous, no await
