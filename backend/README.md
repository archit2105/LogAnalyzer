# CAR-T Platform Q&A — Production (Azure OpenAI edition)

A chat interface that answers operational questions about the CAR-T Cart-Order
Milestone Event Platform by combining three sources:

1. **Logs** — queried LIVE from **New Relic** via the NerdGraph (GraphQL) API.
   There is no offline/synthetic-log mode anymore — this is a direct
   production integration. There's no log-type restriction; all log types
   across the platform's Mulesoft and AWS Lambda services are in scope.
2. **Knowledge base** — Confluence pages, chunked and embedded (Azure
   OpenAI `text-embedding-3-large`) into **PostgreSQL with pgvector** by a
   separate ingestion job. This is the production integration — see
   "Confluence KB (PostgreSQL + pgvector)" below.
3. **Cart metadata** — direct PostgreSQL, split across two data sources
   (CCM a.k.a. COM NA, and COIC). Routed automatically by milestone.

The orchestration layer is **Azure OpenAI** (GPT) with native tool-use. GPT
decides per question which tool(s) to call. This variant is functionally
identical to the sister folder `qa_poc_nr` which uses Anthropic Claude — same
tools, same prompt, different LLM behind them.

---

## How to run

```powershell
cd qa_poc_nr_azure
pip install -r requirements.txt

# Required — Azure OpenAI (4 values, all from the Azure portal)
$env:AZURE_OPENAI_ENDPOINT = "https://YOUR-RESOURCE.openai.azure.com"
$env:AZURE_OPENAI_API_KEY = "..."
$env:AZURE_OPENAI_DEPLOYMENT = "gpt-4o"                   # your DEPLOYMENT name, not model
$env:AZURE_OPENAI_API_VERSION = "2024-08-01-preview"

# Required — New Relic (production account for this environment)
$env:NEW_RELIC_API_KEY = "NRAK-..."
$env:NEW_RELIC_ACCOUNT_ID = "..."
# $env:NEW_RELIC_REGION = "US"      # or "EU"

# Required for cart metadata lookups — direct PostgreSQL, TWO sources.
# Either source can be left unset if you don't need it; that source's
# lookups just return a clean "not configured" error instead of crashing.
$env:CCM_DB_HOST = "ccm-postgres.example.internal"
$env:CCM_DB_PORT = "5432"
$env:CCM_DB_USER = "..."
$env:CCM_DB_PASSWORD = "..."
$env:CCM_DB_NAME = "..."

$env:COIC_DB_HOST = "coic-postgres.example.internal"
$env:COIC_DB_PORT = "5432"
$env:COIC_DB_USER = "..."
$env:COIC_DB_PASSWORD = "..."
$env:COIC_DB_NAME = "..."

# PostgreSQL + pgvector (Confluence KB) — connection details for the
# table(s) the Confluence ingestion job writes chunks/embeddings to.
$env:KB_PG_HOST = "kb-postgres.example.internal"
$env:KB_PG_PORT = "5432"
$env:KB_PG_USER = "..."
$env:KB_PG_PASSWORD = "..."
$env:KB_PG_DB = "..."
$env:KB_PG_TABLE = "confluence_docs"   # or KB_PG_TABLES="table1,table2" for multiple sources
# $env:KB_TOP_K = "8"

# Azure OpenAI embeddings — used to embed the user's query before vector
# search. MUST use the same model the ingestion job embedded chunks with
# (text-embedding-3-large) or distances are meaningless.
$env:AZURE_OPENAI_EMBEDDING_ENDPOINT = "https://YOUR-RESOURCE.openai.azure.com"
$env:AZURE_OPENAI_EMBEDDING_API_KEY = "..."   # omit to reuse AZURE_OPENAI_API_KEY
# $env:AZURE_OPENAI_EMBEDDING_DEPLOYMENT = "text-embedding-3-large"

# Optional — override the fixed service filters / lookback windows if the
# platform's naming convention or AWS account changes. Defaults match the
# current production spec (see "New Relic log source" below).
# $env:NEW_RELIC_MULESOFT_SERVICE_LIKE = "phm%prod"
# $env:NEW_RELIC_LAMBDA_ARN_LIKE = "%947424840714%"
# $env:NEW_RELIC_EXTRA_EXACT_SERVICES = "cquence"
# $env:NEW_RELIC_STEP1_DEFAULT_SINCE = "10 days ago"
# $env:NEW_RELIC_STEP2_DEFAULT_SINCE = "90 days ago"

# Corporate SSL inspection workaround (dev only). Pick one path per endpoint:
# $env:NR_SSL_VERIFY = "false"
# $env:PG_SSL_MODE = "disable"
# $env:AZURE_OPENAI_SSL_VERIFY = "false"
#   OR, the proper fix:
# $env:NR_SSL_CA_BUNDLE = "C:\path\to\corp-ca.pem"
# $env:PG_SSL_CA_BUNDLE = "C:\path\to\corp-ca.pem"
# $env:AZURE_OPENAI_SSL_CA_BUNDLE = "C:\path\to\corp-ca.pem"

# One command starts BOTH the backend (this FastAPI app, port 5000) AND
# the React frontend (../frontend, via `npm run dev`, port 5173) as a
# child process. First install the frontend's deps once:
#   cd ../frontend && npm install && cd ../backend
python app.py

# Prefer to run only the backend (e.g. frontend deployed separately)?
# $env:LAUNCH_FRONTEND = "false"
# python app.py

# Or run the backend with uvicorn's own auto-reloader (backend-only,
# start the frontend yourself in a second terminal with `npm run dev`):
uvicorn app:app --reload --host 0.0.0.0 --port 5000
```

Open the chat UI at <http://localhost:5173> (the React app — this is what
you actually use). The backend itself lives at <http://localhost:5000>;
hitting it directly in a browser just redirects to the frontend. Backend
API docs at <http://localhost:5000/docs>.

Override the ports/paths via env vars if needed: `BACKEND_PORT`,
`FRONTEND_PORT`, `FRONTEND_DIR` (defaults to `../frontend` relative to this
file), `LAUNCH_FRONTEND` (`false` to skip auto-starting the React app).

### Where to find your four Azure OpenAI values

- **AZURE_OPENAI_ENDPOINT** — Azure Portal → your Azure OpenAI resource → *Keys and Endpoint* → Endpoint.
- **AZURE_OPENAI_API_KEY** — same page as endpoint. Use *KEY 1* (or *KEY 2*).
- **AZURE_OPENAI_DEPLOYMENT** — Azure OpenAI Studio → *Deployments*. This is a **custom name** your admin assigned (e.g. `gpt-4o`, `ctx-triage-gpt4`); it is NOT the model name.
- **AZURE_OPENAI_API_VERSION** — a date string like `2024-08-01-preview`. Different versions support different features (parallel tool calls, structured outputs, etc.). Match your admin's recommendation; the default in config.py is a reasonable pick.

If no deployment exists yet: your Azure admin needs to create one in Azure OpenAI Studio → *Deployments* → *Create new deployment*, pick a model (e.g. `gpt-4o`), give it a name. That name is `AZURE_OPENAI_DEPLOYMENT`.

On startup the app:

1. Prints its configuration so you can confirm everything's pointing where you expect.
2. Runs a New Relic healthcheck (counts logs matching the Mulesoft/Lambda service filters in the last day).
3. Fetches one sample record and lists its top-level attributes — useful for confirming how New Relic flattened your ingested JSON.
4. Opens the CCM and COIC PostgreSQL connection pools and runs a smoke-test query against each `order__c` table. Either source can fail independently — if one (or both) is unreachable, the rest of the app still works; only lookups routed to that source are disabled.

If the healthcheck fails, fix it (creds, account ID, service filters) before continuing. The app will still start, but every query will fail until resolved.

### Production deployment

For production, run uvicorn directly with multiple workers behind a reverse proxy:

```bash
uvicorn app:app --host 0.0.0.0 --port 5000 \
    --workers 4 --proxy-headers --forwarded-allow-ips='*'
```

`/healthz` is a liveness probe; `/readyz` is a readiness probe that returns 503 if Azure OpenAI, the KB (PostgreSQL + pgvector), or BOTH Postgres cart-metadata sources (CCM and COIC) are down — it only requires at least one cart-metadata source to be up, since `query_cart_metadata` degrades gracefully per-source. Both are wired for Kubernetes / App Service health checks.

### SSL on corporate networks

If you see `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate` in the logs when calling New Relic, PostgreSQL, or Azure OpenAI, your corporate network has an SSL inspection proxy (Zscaler, Netskope, BlueCoat, etc.) and Python doesn't trust the proxy's root certificate by default. Two ways to fix it:

**Proper fix (production):** Get the corporate root CA `.pem` file from IT and set:

```powershell
$env:NR_SSL_CA_BUNDLE = "C:\path\to\corp-ca.pem"
$env:PG_SSL_CA_BUNDLE = "C:\path\to\corp-ca.pem"
$env:AZURE_OPENAI_SSL_CA_BUNDLE = "C:\path\to\corp-ca.pem"
```

**Dev bypass only:** Disable verification entirely:

```powershell
$env:NR_SSL_VERIFY = "false"
$env:PG_SSL_MODE = "disable"
$env:AZURE_OPENAI_SSL_VERIFY = "false"
```

The proxy is already inspecting your traffic either way, so disabling Python's verification doesn't introduce a new threat on a corporate laptop. **Don't ship this to production.**

---

## New Relic log source

Logs are read directly from live New Relic — there is no offline/synthetic
mode. There is no `logtype` restriction; every log type across the
platform's services is in scope. Logs come from two architectural
components, distinguished by fixed filters:

| Source | Service filter | Tracking field |
|---|---|---|
| Mulesoft applications | `service LIKE 'phm%prod'` | `event` |
| AWS Lambda functions | `faas.arn LIKE '%<aws-account-id>%'` | `correlationId` |

Cart Order IDs cannot be read directly off a failure log, so a **two-step
query/correlation workflow** is used under the hood:

1. **Find failures** — search for the literal string `"Exception Message"`
   across all columns (`allColumnSearch`), scoped to the services above,
   within a time window (default 10 days). Each hit yields a tracking id
   (`event` or `correlationId`).
2. **Correlate to Cart Order ID** — batch-look-up those tracking ids
   (`event`/`correlationId IN (...)`) against a 90-day window to pull each
   record's `data.CARTOrderId` and its milestone (`subject`).

Cart Order IDs follow regional prefixes: `US-XXXXXX` (US) and `EU-XXXXX`
(Europe).

This workflow is exposed to the LLM as two tools:

- **`find_failed_cart_orders(time_window)`** — Q1: total count + the list
  of distinct failed Cart Order IDs for a given window, with per-cart
  failure context (tracking id, service, exception message).
- **`get_cart_order_payload(cart_order_id, milestone)`** — Q2: the full
  payload for a specific Cart Order ID (optionally scoped to one
  milestone/`subject`). If multiple records match, the latest one (by
  timestamp) is returned, enriched with any Exception Message found for
  the same cart.

All NRQL is built server-side from a whitelisted parameter set (identifiers
are restricted to a strict character whitelist; free text like the
`subject` milestone name is quote-escaped) — there's no path for a user
question to inject arbitrary NRQL.

---

## Confluence KB (PostgreSQL + pgvector)

Everything that isn't a New Relic log question or a cart-metadata
(Postgres) question is routed to `search_confluence_kb`, which does a
semantic search against **PostgreSQL with the `pgvector` extension** —
this is the production KB integration (replaces the earlier ChromaDB/
TF-IDF POC setup).

- A separate ingestion job chunks Confluence pages and embeds them
  (Azure OpenAI `text-embedding-3-large`) into a Postgres table. This
  app does not do any chunking, embedding, or ingestion — `kb_store.py`
  only reads.
- The app connects directly via `psycopg2` (connection pooled — see
  `kb_store.py`). Table shape expected: `id, page_id, page_title,
  section, section_id, chunk, chunk_id, embedding, source, metadata,
  page_version, created_at`.
- **Query embedding.** Before searching, the user's query text is
  embedded via the Azure OpenAI embeddings API (`embeddings.py`) using
  the SAME model the ingestion job used (`text-embedding-3-large`) — set
  via `AZURE_OPENAI_EMBEDDING_ENDPOINT` / `_API_KEY` / `_DEPLOYMENT`. If
  these point at a different embedding model than ingestion used,
  distances are meaningless and results will be poor (not necessarily
  erroring — just wrong), so double-check this matches the ingestion
  job's model.
- **Retrieval ranking** (`kb_retrieval.py`, built by the ingestion team):
  a query that essentially names a page's title short-circuits straight
  to that page's intro chunks; otherwise the query is embedded, the
  nearest chunks are pulled via pgvector's `<=>` cosine-distance
  operator, then re-ranked page-by-page (best + average chunk
  similarity, with a title-term bonus) before picking the top chunks
  from the top pages. Placeholder/empty chunks (e.g. `"N/A"`) are
  filtered out.
- Unlike the old TF-IDF setup, this is **real semantic embedding
  search** — natural, descriptive queries work well; no need to reduce
  them to bare keywords.
- The LLM is instructed (see `system_prompt.txt`) to call
  `search_confluence_kb` **more than once per question** when the answer
  spans multiple pages — e.g. business-impact / downstream-system / fix
  questions — rather than settling for a single top-k retrieval.

### Multiple Confluence sources

`KB_PG_TABLES` (comma-separated) or the single-table `KB_PG_TABLE` env
var controls which table(s) are searched. Every table configured is
queried on each `search_confluence_kb` call, and the results are merged
and re-ranked (title-match hits first, then by ascending distance)
before the top-k are returned — you don't need to know which
table/Confluence space holds the answer.

### What's explicitly not built (yet)

- No hybrid (vector + keyword) search, no cross-encoder reranking beyond
  the page/title heuristics in `kb_retrieval.py`.
- No metadata filtering — the `filters` parameter on `search_confluence_kb`
  is accepted for interface compatibility only and currently ignored;
  the schema doesn't expose a filterable metadata path yet.

### Startup check

On boot, the app connects to the KB store and logs the chunk count:

```
[tools] KB store connected: host=kb-postgres.example.internal, db=confluence, tables=['confluence_docs'], 4213 chunk(s).
```

**If the connection fails**, you'll see a warning and `search_confluence_kb`
will return a tool-level error (not a crash) until it's fixed — check
`KB_PG_HOST`/`KB_PG_USER`/`KB_PG_PASSWORD`/`KB_PG_DB` and network/firewall
access to port 5432.

**If the embedding call fails** (e.g. wrong `AZURE_OPENAI_EMBEDDING_ENDPOINT`
or missing API key), `search_confluence_kb` returns an error naming the
missing config — set `AZURE_OPENAI_EMBEDDING_ENDPOINT` and
`AZURE_OPENAI_EMBEDDING_API_KEY` (or confirm `AZURE_OPENAI_API_KEY` is a
valid fallback for that resource).

**If both are configured correctly**, queries will work. Test with:
```bash
curl -X POST http://localhost:5000/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "test",
    "messages": [{"role": "user", "content": "What is Manufacturing Start?"}]
  }'
```

---

## Cart-metadata DB (direct PostgreSQL — CCM + COIC)

The `query_cart_metadata` tool connects directly to PostgreSQL over port
5432 (via `asyncpg`) — there is no REST layer. There are **two separate
data sources**, each with its own connection details:

- **CCM** (also known as COM NA)
- **COIC**

Which one gets queried is decided automatically by the **milestone**
named in the question — see the routing table below. If no milestone is
given, the tool defaults to CCM (documented assumption; pass `source`
explicitly to force COIC instead).

### Tables

Both sources have the same shape of tables, always schema-qualified with
`copystorm.` (configurable via `PG_SCHEMA`, default `copystorm`):

| Source | Order table | Milestone table |
|---|---|---|
| CCM | `copystorm.order__c` | `copystorm.ordermilestones__c` (plural) |
| COIC | `copystorm.order__c` | `copystorm.ordermilestone__c` (singular) |

Join key: `order__c.id` = the milestone table's `order__c` column (a
foreign key back to the parent order — NOT that table's own `id`
column, which is just its own row's primary key). Cart ID lookups use
`order__c.name`. A cart can have many milestone rows; when a specific
milestone is named, the query also filters on the milestone-name column
(`name` for CCM, `milestonename__c` for COIC) to select just that event.

### Columns actually queried (PII-restricted allowlist)

Only these columns are ever selected — nothing else, and no personal
data:

**CCM `order__c`:** `apheresispostatus__c`, `manufacturingstatus__c`, `orderstatus_c`, `manufacturingenddatetime__c`, `manufacturingstartdatetime__c` (actual start), `plannedmanufacturingstartdate__c` (planned start). **`ordercancelled__c` does NOT exist in CCM** — COIC only; don't add it back to the CCM allowlist.

**CCM `ordermilestones__c`:** `id`, `order__c` (FK, join key), `name`, `milestonedatetime__c`.

**COIC `order__c`:** `ordercancelled__c` (mainly a join anchor — most COIC detail lives in the milestone table).

**COIC `ordermilestone__c`:** `id`, `order__c` (FK, join key), `actaldate__c` (actual date — absent means the event hasn't occurred), `planneddate__c`, `milestonename__c`.

### Milestone → source routing

| Source | Milestones |
|---|---|
| **CCM** | FDP Batch/Lot ID · Apheresis Collection Complete · Cryopreservation Process End · Manufacturing Start · Manufacturing End · COI/COC Order Created · FP Labeling · Order Approved · Cryopreserved Apheresis Received at Manufacturing Site · Cryopreserved Apheresis QA Released at Manufacturing Site |
| **COIC** | Receipt at Cryopreservation Site · Receipt at Manufacturing Site · FP QA Release at Manufacturing Site · FP Drop Off at Infusion Site · FP Receipt at Infusion Site · Shipment Preparation at Collection Site · Shipment Preparation at Manufacturing Site · Fresh Apheresis PickedUp from Treatment Center · Shipment Preparation at Cryopreservation Site · FP Pick Up from Manufacturing Site · Fresh Apheresis DroppedOff at CryoSite · Cryopreserved Apheresis Pick Up · Cryopreserved Apheresis Drop Off at Manufacturing Site |
| **New Relic** (not Postgres) | Purchase Order for CMO · Purchase Order Acknowledgement · Advanced Shipment Notice · Goods Receipt Retain bag · Goods Receipt (Cryo Aph) · Goods Receipt Finished Product · Finished Goods Batch Master · Patient Enrollment Complete |

Milestones in the last row aren't in Postgres at all — `query_cart_metadata` returns a routing note for those, telling the agent to call `get_cart_order_payload` instead and extract only metadata-looking fields (never the full log by default — see the response-handling rules in `system_prompt.txt`).

### Setup

1. Provision (or get connection details for) the CCM and COIC PostgreSQL databases from your DBA/platform team.
2. Set the env vars for whichever source(s) you need:

   ```powershell
   $env:CCM_DB_HOST = "ccm-postgres.example.internal"
   $env:CCM_DB_PORT = "5432"
   $env:CCM_DB_USER = "..."
   $env:CCM_DB_PASSWORD = "..."
   $env:CCM_DB_NAME = "..."

   $env:COIC_DB_HOST = "coic-postgres.example.internal"
   $env:COIC_DB_PORT = "5432"
   $env:COIC_DB_USER = "..."
   $env:COIC_DB_PASSWORD = "..."
   $env:COIC_DB_NAME = "..."
   ```

3. **Restart** `python app.py`. Look for `[db:ccm] Connected to PostgreSQL (...)` and `[db:coic] Connected to PostgreSQL (...)` in the logs — each source logs independently.

### Troubleshooting

- **`CCM_DB_HOST / CCM_DB_USER / CCM_DB_NAME environment variables are not fully set`** — env vars didn't make it to the uvicorn process, or you only need COIC and can ignore this (CCM lookups will just be unavailable).
- **`Could not connect to the CCM PostgreSQL database at host:port/db: ...`** — check host/port reachability (firewall, VPN) and credentials. Direct Postgres (5432) may need a firewall exception if it was previously blocked in favor of an HTTPS-only setup — confirm your network allows outbound 5432 to the DB host, or use `PG_SSL_CA_BUNDLE`/`PG_SSL_MODE` if it's an SSL-inspecting proxy issue rather than a port block.
- **A milestone-based question returns an empty `milestones` list** — per the spec, this means the event hasn't occurred yet, not an error. The agent is instructed to say so rather than guess a date.

If either `*_DB_HOST` is unset, or that source is unreachable at startup, the agent still runs — lookups routed to that source return a graceful error to GPT, and only that source's data is unavailable (the other source, the log tools, and `search_confluence_kb` are unaffected). The same graceful-degradation pattern applies if the KB's PostgreSQL/pgvector store isn't reachable.

---

## Architecture

```
              Browser (chat UI)
                     │
                     ▼
            FastAPI /api/chat
                     │
                     ▼
           Azure OpenAI (GPT)
              with tool_use
     ┌───────┬─────────────┬───────────────────┐
     ▼       ▼             ▼                   ▼
find_failed  get_cart      search_confluence_kb  query_cart_metadata
_cart_orders _order_payload      │                   │
     │            │              ▼                   ▼
     └────┬───────┘        PostgreSQL+pgvector  PostgreSQL
          ▼                (Confluence chunks,   (CCM + COIC pools,
   New Relic NerdGraph      separate ingestion    routed by milestone)
   (2-step NRQL workflow)   job embeds w/
                            text-embedding-3-large)
```

**Why two log tools, not one?** Q1 (how many/which carts failed) and Q2
(full payload for a specific cart) need different NRQL shapes and
different default lookback windows — splitting them keeps each tool's
schema simple and keeps the LLM from having to reconstruct the two-step
workflow itself.

**Why parameterized NRQL, not "GPT writes NRQL"?** Safer (no NRQL
injection), faster (no extra LLM hop), and avoids GPT needing to learn
your account's specific attribute-naming convention. Every NRQL the app
generates is built server-side from a whitelisted parameter set.

---

## File map

| File | Role |
|---|---|
| `app.py` | FastAPI (async) server, agent loop, GPT tool-use orchestration |
| `system_prompt.txt` | The system prompt — routing rules and answer format. Edit without touching code. |
| `tools.py` | The four tools (`find_failed_cart_orders`, `get_cart_order_payload`, `search_confluence_kb`, `query_cart_metadata`) and their schemas. |
| `nr_client.py` | New Relic integration: the two-step NRQL builders, async NerdGraph executor (httpx), in-memory TTL cache, healthcheck |
| `db_client.py` | Direct PostgreSQL integration (`asyncpg`) for CCM + COIC — two independent connection pools, milestone-based routing. Graceful disabled state per-source if creds absent. |
| `pgvector_client.py` | The module tools.py talks to for KB search — merges/re-ranks results across `KB_PG_TABLES`, caches, exposes `is_available()`/`search()`. |
| `kb_retrieval.py` | Retrieval ranking logic (title-match short-circuit, page/chunk scoring, noise filtering) — built by the ingestion team for this schema. |
| `kb_store.py` | Read-only PostgreSQL + pgvector queries (`psycopg2`, connection pool) — nearest-neighbor search, page titles, page intro chunks. |
| `embeddings.py` | Azure OpenAI embeddings client (`text-embedding-3-large`) used to embed queries before vector search. |
| `config.py` | Central config with env-var overrides |
| `../frontend` | React (Vite) chat UI — separate process/port, see top-level README |
| `requirements.txt` | `fastapi`, `uvicorn`, `openai`, `httpx`, `asyncpg`, `psycopg2-binary`, `pgvector`, `requests` |

> There is no `data/knowledge_base.md` or KB-chunking code in this repo
> anymore — the old synthetic/TF-IDF knowledge base (and, before that,
> ChromaDB) has been fully removed. Confluence content now lives in
> PostgreSQL with pgvector, populated by a separate ingestion job (not
> part of this app).

---

## Defensive behavior

### Caching

NRQL results are cached for 120 seconds in memory, keyed by the exact NRQL
string. Override with `$env:NRQL_CACHE_TTL_SECONDS = "0"` to disable.

### NRQL injection prevention

Identifiers (tracking ids, Cart Order IDs, SINCE clauses) are validated
against a strict whitelist before being placed in NRQL. Free text (the
`subject` milestone name) is quote-escaped instead, since it legitimately
contains spaces. Anything outside these rules is rejected before the query
leaves your machine — the user has no path to inject arbitrary NRQL.

---

## Example questions

**Q1 — failure count / list (New Relic):**
- "How many Cart Order IDs failed in the last 10 days?"
- "Which carts failed today?"
- "List the failed cart orders this week."

**Q2 — specific cart payload (New Relic):**
- "Give me the payload for US-000815."
- "What's the Apheresis Collection Complete payload for EU-00081?"
- "What went wrong with US-000815?"

**Confluence knowledge (PostgreSQL + pgvector-backed):**
- "What does <error/exception> mean?"
- "Explain the platform's architecture."
- "What's the business impact if <system> is down?"
- "What downstream systems depend on <system>?"
- "How do I fix <error>?"

**Cart metadata (PostgreSQL — CCM/COIC-backed):**
- "Is US-000815 cancelled?"
- "Has US-000815 hit Manufacturing Start yet?"
- "Which carts are overdue on manufacturing?"

---

## Troubleshooting

**Healthcheck reports 0 matching logs.** Confirm in the New Relic UI that a query like `SELECT count(*) FROM Log WHERE service LIKE 'phm%prod' OR faas.arn LIKE '%<aws-account-id>%' SINCE 1 day ago` returns records. Check `NEW_RELIC_ACCOUNT_ID` and `NEW_RELIC_REGION` (US vs EU) match your account, and that `NEW_RELIC_MULESOFT_SERVICE_LIKE` / `NEW_RELIC_LAMBDA_ARN_LIKE` match this environment's naming.

**HTTP 401 / 403 from NerdGraph.** API key is wrong, expired, or doesn't have NerdGraph permissions. Generate a new User key (not an Ingest key) in New Relic → user menu → API keys.

**Queries return no Cart Order ID for a known failure.** `data.CARTOrderId` may be nested differently than expected for a given ingestion path — `nr_client.get_cart_order_id` falls back to `messageObject.data.CARTOrderId` if the flattened top-level key isn't present. If neither is present, share a sample raw record and we can adjust the helper.

**SSL errors on Windows.** Corporate-proxy issue. Use `pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org -r requirements.txt` or set `SSL_CERT_FILE` to your corporate CA bundle path.
