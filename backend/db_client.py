"""
db_client.py — direct PostgreSQL cart-metadata access, TWO data sources.

Replaces the old Supabase REST-API client. Metadata now lives in
PostgreSQL directly, split across two independent data sources:

    CCM   (also known as COM NA)
    COIC

Which one to query is decided by the MILESTONE the user asks about (see
MILESTONE_SOURCE_MAP below). A few milestones aren't in Postgres at all —
their metadata lives in New Relic logs, and callers should route those to
get_cart_order_payload() instead (tools.py handles that routing).

Tables (always schema-qualified with `copystorm.` per requirement):

    CCM:
        copystorm.order__c
        copystorm.ordermilestones__c      (plural "milestones")

    COIC:
        copystorm.order__c
        copystorm.ordermilestone__c       (singular "milestone")

Flow for a cart lookup, either source:
    1. order__c.name  = the Cart ID  -> find the row, take its `id`.
    2. ordermilestone(s)__c.id (FK)  = order__c.id -> pull milestone rows.

PII: only an explicit allowlist of business columns is ever selected —
see _CCM_ORDER_FIELDS / _COIC_ORDER_FIELDS / *_MILESTONE_FIELDS below. No
personally-identifying columns are queried, by design.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional, Iterable

import config

try:
    import asyncpg
    _ASYNCPG_AVAILABLE = True
except ImportError:
    _ASYNCPG_AVAILABLE = False


# --------------------------------------------------------------------------
# Milestone -> source routing
# --------------------------------------------------------------------------
# Normalized (lowercased) milestone name -> "ccm" | "coic" | "new_relic".
# Built once at import time from the lists below so lookups are O(1).

_CCM_MILESTONES = [
    "FDP Batch/Lot ID",
    "Apheresis Collection Complete",
    "Cryopreservation Process End",
    "Manufacturing Start",
    "Manufacturing End",
    "COI/COC Order Created",
    "FP Labeling",
    "Order Approved",
    "Cryopreserved Apheresis Received at Manufacturing Site",
    "Cryopreserved Apheresis QA Released at Manufacturing Site",
]

_COIC_MILESTONES = [
    "Receipt at Cryopreservation Site",
    "Receipt at Manufacturing Site",
    "FP QA Release at Manufacturing Site",
    "FP Drop Off at Infusion Site",
    "FP Receipt at Infusion Site",
    "Shipment Preparation at Collection Site",
    "Shipment Preparation at Manufacturing Site",
    "Fresh Apheresis PickedUp from Treatment Center",
    "Shipment Preparation at Cryopreservation Site",
    "FP Pick Up from Manufacturing Site",
    "Fresh Apheresis DroppedOff at CryoSite",
    "Cryopreserved Apheresis Pick Up",
    "Cryopreserved Apheresis Drop Off at Manufacturing Site",
]

_NEW_RELIC_MILESTONES = [
    "Purchase Order for CMO",
    "Purchase Order Acknowledgement",
    "Advanced Shipment Notice",
    "Goods Receipt Retain bag",
    "Goods Receipt (Cryo Aph)",
    "Goods Receipt Finished Product",
    "Finished Goods Batch Master",
    "Patient Enrollment Complete",
]

MILESTONE_SOURCE_MAP = {
    **{m.lower(): "ccm" for m in _CCM_MILESTONES},
    **{m.lower(): "coic" for m in _COIC_MILESTONES},
    **{m.lower(): "new_relic" for m in _NEW_RELIC_MILESTONES},
}


def resolve_milestone_source(milestone: Optional[str]) -> Optional[str]:
    """
    Return "ccm", "coic", "new_relic", or None (milestone not given, or
    not recognized — caller should fall back to a default / ask).
    """
    if not milestone:
        return None
    return MILESTONE_SOURCE_MAP.get(milestone.strip().lower())


def milestone_catalog() -> dict:
    """Exposed for a tool schema / debugging — full milestone -> source map."""
    return {
        "ccm": list(_CCM_MILESTONES),
        "coic": list(_COIC_MILESTONES),
        "new_relic": list(_NEW_RELIC_MILESTONES),
    }


# --------------------------------------------------------------------------
# Column allowlists — PII guard + single place to update if schema changes.
# Only these columns are ever selected. `id` and `name` are the join
# keys and are always included regardless of what's requested.
# --------------------------------------------------------------------------
_CCM_ORDER_FIELDS = {
    "id",
    "name",                              # Cart ID
    "apheresispostatus__c",
    "manufacturingstatus__c",
    "orderstatus_c",
    "manufacturingenddatetime__c",
    "manufacturingstartdatetime__c",      # acts like actual manufacturing start date
    "plannedmanufacturingstartdate__c",   # planned manufacturing start date
    # NOTE: ordercancelled__c does NOT exist in CCM's order__c (confirmed
    # against the real schema) — do not add it back here. It exists only
    # in COIC. Selecting it against CCM raises "column does not exist"
    # for every query, which previously surfaced to the user as a
    # generic "database unreachable" error.
}
_CCM_MILESTONE_FIELDS = {
    "id",
    "order__c",       # FK back to order__c.id — NOT the same as this table's own `id`
    "name",
    "milestonedatetime__c",
}

_COIC_ORDER_FIELDS = {
    "id",
    "name",                              # Cart ID
    "ordercancelled__c",
}
_COIC_MILESTONE_FIELDS = {
    "id",
    "order__c",           # FK back to order__c.id — NOT the same as this table's own `id`
    "actaldate__c",       # actual date the event occurred (source schema's own spelling)
    "planneddate__c",
    "milestonename__c",
}

_SCHEMA = config.PG_SCHEMA


def _order_table(source: str) -> str:
    return f"{_SCHEMA}.order__c"


def _milestone_table(source: str) -> str:
    return f"{_SCHEMA}.ordermilestones__c" if source == "ccm" else f"{_SCHEMA}.ordermilestone__c"


def _order_fields(source: str) -> set:
    return _CCM_ORDER_FIELDS if source == "ccm" else _COIC_ORDER_FIELDS


def _milestone_fields(source: str) -> set:
    return _CCM_MILESTONE_FIELDS if source == "ccm" else _COIC_MILESTONE_FIELDS


# --------------------------------------------------------------------------
# Module state — one connection pool per source. Each is independently
# optional; if a source's env vars aren't set, that source is disabled
# (clean error returned to the caller) without affecting the other.
# --------------------------------------------------------------------------
_POOLS: dict = {"ccm": None, "coic": None}
_INIT_ERRORS: dict = {"ccm": None, "coic": None}


def _source_config(source: str) -> dict:
    prefix = "CCM" if source == "ccm" else "COIC"
    return {
        "host": getattr(config, f"{prefix}_DB_HOST"),
        "port": getattr(config, f"{prefix}_DB_PORT"),
        "user": getattr(config, f"{prefix}_DB_USER"),
        "password": getattr(config, f"{prefix}_DB_PASSWORD"),
        "database": getattr(config, f"{prefix}_DB_NAME"),
    }


async def _init_source(source: str) -> None:
    global _POOLS, _INIT_ERRORS

    if not _ASYNCPG_AVAILABLE:
        _INIT_ERRORS[source] = (
            "The `asyncpg` library isn't available. Run "
            "`pip install -r requirements.txt` to install it."
        )
        print(f"[db:{source}] {_INIT_ERRORS[source]}")
        return

    cfg = _source_config(source)
    if not cfg["host"] or not cfg["user"] or not cfg["database"]:
        _INIT_ERRORS[source] = (
            f"{source.upper()}_DB_HOST / {source.upper()}_DB_USER / "
            f"{source.upper()}_DB_NAME environment variables are not fully "
            f"set. Metadata lookups routed to '{source}' will be unavailable "
            f"until they are. See the README for setup."
        )
        print(f"[db:{source}] {_INIT_ERRORS[source]}")
        return

    ssl_arg = config.PG_SSL_CA_BUNDLE or (config.PG_SSL_MODE if config.PG_SSL_MODE else None)

    try:
        pool = await asyncpg.create_pool(
            host=cfg["host"],
            port=cfg["port"],
            user=cfg["user"],
            password=cfg["password"],
            database=cfg["database"],
            min_size=1,
            max_size=5,
            command_timeout=10,
            ssl=ssl_arg if ssl_arg not in ("disable", None) else None,
        )
        # Smoke test.
        async with pool.acquire() as conn:
            await conn.fetchval(f"SELECT 1 FROM {_order_table(source)} LIMIT 1")
        _POOLS[source] = pool
        print(f"[db:{source}] Connected to PostgreSQL ({cfg['host']}:{cfg['port']}/{cfg['database']}).")
    except Exception as exc:
        _INIT_ERRORS[source] = (
            f"Could not connect to the {source.upper()} PostgreSQL database "
            f"at {cfg['host']}:{cfg['port']}/{cfg['database']}: {exc}"
        )
        print(f"[db:{source}] {_INIT_ERRORS[source]}")
        _POOLS[source] = None


async def initialize() -> None:
    """Bring up both source pools independently. Call once at startup."""
    await _init_source("ccm")
    await _init_source("coic")


def is_available(source: str) -> bool:
    return _POOLS.get(source) is not None


async def close_pools() -> None:
    for source, pool in list(_POOLS.items()):
        if pool is not None:
            await pool.close()
            _POOLS[source] = None


# Kept for backward-compat with app.py/tools.py shutdown calls that expect
# this name from the old Supabase client.
async def close_http_client() -> None:
    await close_pools()


def _row_to_dict(row) -> dict:
    """asyncpg Records -> plain dict, JSON-safe (dates/datetimes -> isoformat)."""
    out = {}
    for k, v in dict(row).items():
        if isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------
# fetch_cart_metadata — order__c (+ optionally its milestone rows)
# --------------------------------------------------------------------------
async def fetch_cart_metadata(
    cart_id: str,
    milestone: Optional[str] = None,
    source: Optional[str] = None,
    fields: Optional[Iterable[str]] = None,
) -> dict:
    """
    Look up cart-level metadata (order__c) and, if a milestone is given
    or the caller wants milestone rows, the matching milestone-table rows.

    Source resolution, in order:
      1. Explicit `source` arg ("ccm" or "coic"), if given.
      2. Resolved from `milestone` via MILESTONE_SOURCE_MAP, if the
         milestone maps to "ccm" or "coic".
      3. If milestone resolves to "new_relic": return a routing note
         immediately — this tool doesn't have that data, New Relic does.
      4. Default: "ccm" (order__c carries the primary manufacturing/
         status fields there) — documented assumption when no milestone
         or source was given.

    Returns:
        {"found": True, "source": "ccm"|"coic", "order": {...},
         "milestones": [...] (only if milestone/rows requested),
         "note": "..." (optional)}
        {"found": False, "note": "..."}
        {"error": "...", "found": False}
        {"routed": True, "source": "new_relic", "note": "..."}  — for
            milestones whose metadata lives in New Relic, not Postgres.
    """
    if not cart_id:
        return {"error": "cart_id is required.", "found": False}

    milestone_source = resolve_milestone_source(milestone)
    if milestone_source == "new_relic":
        return {
            "routed": True,
            "source": "new_relic",
            "milestone": milestone,
            "note": (
                f"'{milestone}' metadata comes from New Relic logs, not "
                f"PostgreSQL. Call get_cart_order_payload(cart_order_id="
                f"'{cart_id}', milestone='{milestone}') instead, and "
                f"extract only metadata-looking fields from the response — "
                f"do not return the entire log."
            ),
        }

    resolved_source = source or milestone_source or "ccm"
    if resolved_source not in ("ccm", "coic"):
        return {
            "error": f"Unknown source: {resolved_source!r}. Use 'ccm' or 'coic'.",
            "found": False,
        }

    if not is_available(resolved_source):
        return {
            "error": _INIT_ERRORS.get(resolved_source)
                      or f"{resolved_source.upper()} PostgreSQL is not initialized.",
            "found": False,
        }

    order_fields = _order_fields(resolved_source)
    wanted = {f for f in (fields or [])} & order_fields if fields else set(order_fields)
    wanted |= {"id", "name"}  # always include join keys
    select_clause = ", ".join(sorted(wanted))

    pool = _POOLS[resolved_source]
    try:
        async with pool.acquire() as conn:
            order_row = await conn.fetchrow(
                f"SELECT {select_clause} FROM {_order_table(resolved_source)} "
                f"WHERE name = $1 LIMIT 1",
                cart_id,
            )
    except Exception as exc:
        return {
            "error": f"{resolved_source.upper()} PostgreSQL query failed: {exc}",
            "found": False,
        }

    if order_row is None:
        return {
            "found": False,
            "source": resolved_source,
            "note": (
                f"No order__c row exists for Cart ID {cart_id!r} in the "
                f"{resolved_source.upper()} database. The cart may live in "
                f"the other Postgres source, or may not be registered in "
                f"either yet."
            ),
        }

    order_dict = _row_to_dict(order_row)
    result = {"found": True, "source": resolved_source, "order": order_dict}

    # Pull milestone rows if a milestone was named, or if the cart's
    # order__c row was found and milestone-level detail is useful by
    # default. We only join to milestones when a milestone name was
    # explicitly given OR the caller didn't restrict fields (i.e. wants
    # the full picture) — narrow `fields` requests skip the join for
    # efficiency unless milestone is explicitly named.
    order_id = order_dict.get("id")
    if order_id and (milestone or not fields):
        milestone_result = await _fetch_milestones(
            resolved_source, order_id, milestone_name=milestone,
        )
        result["milestones"] = milestone_result.get("rows", [])
        if milestone and not milestone_result.get("rows"):
            result["note"] = (
                f"No '{milestone}' entry exists in "
                f"{_milestone_table(resolved_source)} for this cart — "
                f"this means the event has not occurred yet."
            )

    return result


async def _fetch_milestones(
    source: str, order_id, milestone_name: Optional[str] = None,
) -> dict:
    """
    Internal: milestone-table rows for a given order__c.id.

    IMPORTANT: the join key is the milestone table's `order__c` column
    (a foreign key back to order__c.id) — NOT that table's own `id`
    column, which is just its own row's primary key and has nothing to
    do with order__c.id. A cart can have many milestone rows, so when a
    specific milestone is named we also filter on the milestone-name
    column to select just that one event.
    """
    if not is_available(source):
        return {"error": _INIT_ERRORS.get(source), "rows": []}

    fields_wanted = _milestone_fields(source)
    select_clause = ", ".join(sorted(fields_wanted))
    name_col = "milestonename__c" if source == "coic" else "name"

    pool = _POOLS[source]
    try:
        async with pool.acquire() as conn:
            if milestone_name:
                rows = await conn.fetch(
                    f"SELECT {select_clause} FROM {_milestone_table(source)} "
                    f"WHERE order__c = $1 AND {name_col} ILIKE $2",
                    order_id, milestone_name,
                )
            else:
                rows = await conn.fetch(
                    f"SELECT {select_clause} FROM {_milestone_table(source)} "
                    f"WHERE order__c = $1",
                    order_id,
                )
    except Exception as exc:
        return {"error": f"{source.upper()} milestone query failed: {exc}", "rows": []}

    return {"rows": [_row_to_dict(r) for r in rows]}


# --------------------------------------------------------------------------
# find_overdue_carts — CCM only (manufacturing dates live in CCM's
# order__c; COIC's order__c has no manufacturing schedule columns).
# --------------------------------------------------------------------------
async def find_overdue_carts(fields: Optional[Iterable[str]] = None) -> dict:
    """
    CCM-only: every cart whose manufacturingenddatetime__c has passed but
    manufacturingstatus__c is still 'IN-PROGRESS'.
    """
    source = "ccm"
    if not is_available(source):
        return {
            "error": _INIT_ERRORS.get(source) or "CCM PostgreSQL is not initialized.",
            "found": False,
        }

    wanted = {f for f in (fields or [])} & _CCM_ORDER_FIELDS if fields else set(_CCM_ORDER_FIELDS)
    wanted |= {"id", "name", "manufacturingenddatetime__c", "manufacturingstatus__c"}
    select_clause = ", ".join(sorted(wanted))
    today = date.today()

    pool = _POOLS[source]
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {select_clause} FROM {_order_table(source)} "
                f"WHERE manufacturingenddatetime__c < $1 "
                f"AND manufacturingstatus__c = 'IN-PROGRESS' "
                f"ORDER BY manufacturingenddatetime__c ASC",
                today,
            )
    except Exception as exc:
        return {"error": f"CCM PostgreSQL query failed: {exc}", "found": False}

    if not rows:
        return {
            "rows": [],
            "found": False,
            "count": 0,
            "as_of": today.isoformat(),
            "note": (
                f"No carts have a passed manufacturingenddatetime__c with "
                f"manufacturingstatus__c still 'IN-PROGRESS' as of "
                f"{today.isoformat()}. That's the healthy state."
            ),
        }

    return {
        "rows": [_row_to_dict(r) for r in rows],
        "found": True,
        "count": len(rows),
        "as_of": today.isoformat(),
    }
