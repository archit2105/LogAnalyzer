"""
app.py — FastAPI server hosting the chat UI and the agent that orchestrates
Azure OpenAI (GPT) with the three tools defined in tools.py.

This is the AZURE OPENAI variant of the POC. The sibling folder
(qa_poc_nr) uses Anthropic Claude with the same tool layer. The tool
implementations, PostgreSQL client, and New Relic client are identical
across the two folders — only the LLM wiring differs.

Architecture (unchanged from the Claude variant):
    User question (from the React frontend in ../frontend, or the
    Chrome extension)
       v
    Azure OpenAI (orchestrator) — picks tool(s), reasons over results
       v
    Tool calls (find_failed_cart_orders / get_cart_order_payload /
                search_confluence_kb / query_cart_metadata) — agent loop
       v
    Tool results returned to Azure OpenAI
       v
    GPT synthesizes the final answer and the agent returns to the UI.

Run for development:
    pip install -r requirements.txt
    (cd ../frontend && npm install)             # once, for the React app
    $env:AZURE_OPENAI_ENDPOINT = "https://YOUR-RESOURCE.openai.azure.com"
    $env:AZURE_OPENAI_API_KEY = "..."
    $env:AZURE_OPENAI_DEPLOYMENT = "gpt-4o"     # your deployment name
    $env:AZURE_OPENAI_API_VERSION = "2024-08-01-preview"
    $env:CCM_DB_HOST = "ccm-postgres.example.com"
    $env:CCM_DB_USER = "..."
    $env:CCM_DB_PASSWORD = "..."
    $env:CCM_DB_NAME = "..."
    $env:COIC_DB_HOST = "coic-postgres.example.com"
    $env:COIC_DB_USER = "..."
    $env:COIC_DB_PASSWORD = "..."
    $env:COIC_DB_NAME = "..."
    $env:KB_PG_HOST = "kb-postgres.example.com"
    $env:KB_PG_USER = "..."
    $env:KB_PG_PASSWORD = "..."
    $env:KB_PG_DB = "..."
    $env:KB_PG_TABLE = "confluence_docs"
    $env:AZURE_OPENAI_EMBEDDING_ENDPOINT = "https://YOUR-RESOURCE.openai.azure.com"
    $env:AZURE_OPENAI_EMBEDDING_API_KEY = "..."   # or omit to reuse AZURE_OPENAI_API_KEY
    python app.py
    # -> backend on http://localhost:5000, React UI auto-started on
    #    http://localhost:5173 (open that one in your browser)

Notes on the differences from the Claude variant:

1. TOOL SCHEMA SHAPE: OpenAI expects tools wrapped in {type: "function",
   function: {...}}. We use TOOL_SCHEMAS_OPENAI from tools.py.
2. SYSTEM PROMPT PLACEMENT: OpenAI takes the system prompt as the first
   entry in `messages` (role="system"), not as a separate parameter.
3. TOOL_CHOICE: OpenAI's finish_reason is "tool_calls" (plural) when the
   model wants to invoke tools; Claude's is "tool_use".
4. TOOL RESULT MESSAGES: OpenAI wants one message per tool call, with
   role="tool" and a tool_call_id linking back. Claude wants a single
   user-role message containing a list of tool_result blocks.
5. TOOL ARGUMENTS ARE A STRING: OpenAI returns tool call arguments as a
   JSON string (`function.arguments`), NOT a parsed dict. We json.loads
   defensively and treat malformed JSON as a tool error the model can
   see and recover from.
"""

import os
import json
import time
import asyncio
import logging
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from openai import AsyncAzureOpenAI

import tools
import config
import nr_client


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)
log = logging.getLogger("ctx.app")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
DEPLOYMENT = config.AZURE_OPENAI_DEPLOYMENT
MAX_TOOL_ITERATIONS = 8
MAX_TOKENS = 4096
PROMPT_FILE = Path(__file__).resolve().parent / "system_prompt.txt"

# --------------------------------------------------------------------------
# Frontend (React/Vite) — runs as its OWN process on its OWN port.
# `python app.py` starts the backend (this FastAPI/uvicorn app, port
# BACKEND_PORT) AND spawns `npm run dev` for the React app (port
# FRONTEND_PORT) as a child process, so a single command brings up both.
#
# The React dev server (see frontend/vite.config.js) already proxies
# `/api/*` calls to http://localhost:<BACKEND_PORT>, and this app's CORS
# middleware below allows cross-origin calls too, so the UI works whether
# it's opened directly against the Vite port or reached through the proxy.
#
# Override any of these via env vars if your layout / ports differ.
# --------------------------------------------------------------------------
BACKEND_PORT = int(os.environ.get("BACKEND_PORT", "5000"))
FRONTEND_PORT = int(os.environ.get("FRONTEND_PORT", "5173"))
FRONTEND_DIR = Path(
    os.environ.get("FRONTEND_DIR", str(Path(__file__).resolve().parent.parent / "frontend"))
).resolve()
# Set LAUNCH_FRONTEND=false to run only the backend (e.g. in prod, where the
# React app is built and deployed separately).
LAUNCH_FRONTEND = os.environ.get("LAUNCH_FRONTEND", "true").strip().lower() not in (
    "false", "0", "no", "off",
)


# --------------------------------------------------------------------------
# System prompt
# --------------------------------------------------------------------------
SYSTEM_PROMPT = PROMPT_FILE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Pydantic models for request/response validation
# --------------------------------------------------------------------------
class ChatMessage(BaseModel):
    role: str = Field(..., description="'user' or 'assistant'")
    content: Any


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., min_length=1)


class ToolTraceEntry(BaseModel):
    tool: str
    input: dict
    result_summary: str
    duration_ms: int


class ChatResponse(BaseModel):
    answer: str
    tool_trace: list[ToolTraceEntry]
    iterations: int


class HealthResponse(BaseModel):
    status: str
    components: dict


# --------------------------------------------------------------------------
# Lifespan
# --------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("=" * 70)
    log.info("CTX Cart-Order Q&A (Azure OpenAI variant) — startup")
    log.info("=" * 70)
    for k, v in config.summary().items():
        log.info("  %s: %s", k, v)
    log.info("=" * 70)

    # Validate all four Azure config values before starting anything else.
    # Failing fast here is much clearer than crashing later inside the SDK.
    missing = []
    if not config.AZURE_OPENAI_ENDPOINT:
        missing.append("AZURE_OPENAI_ENDPOINT")
    if not config.AZURE_OPENAI_API_KEY:
        missing.append("AZURE_OPENAI_API_KEY")
    if not config.AZURE_OPENAI_DEPLOYMENT:
        missing.append("AZURE_OPENAI_DEPLOYMENT")
    if not config.AZURE_OPENAI_API_VERSION:
        missing.append("AZURE_OPENAI_API_VERSION")
    if missing:
        raise RuntimeError(
            f"Azure OpenAI configuration is incomplete. Missing env vars: "
            f"{', '.join(missing)}. See config.py for what each one is and "
            f"where to find it in the Azure portal."
        )

    # Build the async client and stash on app.state. AsyncAzureOpenAI takes
    # the endpoint + api version at construction time; the deployment name
    # is passed per-request as the `model` parameter.
    #
    # SSL verification: same corporate-inspection-proxy story as nr_client
    # and db_client. Three options via env vars, in order of "right":
    #   1. AZURE_OPENAI_SSL_CA_BUNDLE = path to corporate root CA .pem
    #   2. AZURE_OPENAI_SSL_VERIFY = "false" — POC bypass
    #   3. Neither set — strict verification (production default)
    #
    # The openai SDK doesn't take verify=... directly, so we build our own
    # httpx.AsyncClient and pass it in.
    import httpx
    ca_bundle = os.environ.get("AZURE_OPENAI_SSL_CA_BUNDLE", "").strip()
    verify_env = os.environ.get("AZURE_OPENAI_SSL_VERIFY", "").strip().lower()
    if ca_bundle:
        verify: object = ca_bundle
        log.info("Using corporate CA bundle for Azure OpenAI: %s", ca_bundle)
    elif verify_env in ("false", "0", "no", "off"):
        verify = False
        log.warning(
            "AZURE_OPENAI_SSL_VERIFY is disabled. Traffic to Azure OpenAI "
            "is unverified. POC only — do not use in production. Set "
            "AZURE_OPENAI_SSL_CA_BUNDLE to the corporate root CA for the "
            "proper fix."
        )
    else:
        verify = True

    app.state.gpt = AsyncAzureOpenAI(
        azure_endpoint=config.AZURE_OPENAI_ENDPOINT,
        api_key=config.AZURE_OPENAI_API_KEY,
        api_version=config.AZURE_OPENAI_API_VERSION,
        http_client=httpx.AsyncClient(verify=verify, timeout=60.0),
    )
    app.state.startup_time = time.time()

    await tools.initialize()

    log.info("[setup] Running New Relic healthcheck...")
    ok, msg, sample = await nr_client.healthcheck()
    if ok:
        log.info("[setup] %s", msg)
        if sample:
            keys = sorted(sample.keys())
            log.info("[setup] Sample record has %d attributes. First 20:", len(keys))
            for k in keys[:20]:
                log.info("          - %s", k)
            if len(keys) > 20:
                log.info("          ... and %d more", len(keys) - 20)
    else:
        log.warning("[setup] %s", msg)
        log.warning(
            "[setup] The app will still start, but queries will fail "
            "until this is resolved."
        )

    log.info("[setup] Ready.")
    yield

    log.info("Shutting down...")
    if hasattr(app.state, "gpt"):
        await app.state.gpt.close()
    await nr_client.close_http_client()
    await tools.shutdown()
    log.info("Shutdown complete.")


# --------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------
app = FastAPI(
    title="CTX Cart-Order Triage Assistant (Azure OpenAI)",
    description=(
        "Agentic Q&A over the CAR-T cart-order milestone platform. "
        "Backed by Azure OpenAI (GPT) with four tools: "
        "find_failed_cart_orders and get_cart_order_payload (New Relic, "
        "live production logs), search_confluence_kb (Confluence pages "
        "in PostgreSQL + pgvector), and query_cart_metadata (PostgreSQL: CCM/COIC)."
    ),
    version="3.0.0-azure",
    lifespan=lifespan,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # POC default. Tighten for production.
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    max_age=86400,
)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def index():
    """
    The chat UI is now the React app in ../frontend, served by its own
    Vite dev server on FRONTEND_PORT (started automatically by this
    process — see __main__ below). This backend no longer serves any
    HTML itself, so hitting "/" just redirects to the React app.
    """
    return RedirectResponse(url=f"http://localhost:{FRONTEND_PORT}/")


@app.get("/healthz", response_model=HealthResponse, tags=["ops"])
async def healthz(request: Request):
    return HealthResponse(
        status="ok",
        components={
            "uptime_seconds": int(time.time() - request.app.state.startup_time),
            "deployment": DEPLOYMENT,
            "log_source": "newrelic",
        },
    )


@app.get("/readyz", tags=["ops"])
async def readyz(request: Request):
    import db_client
    components = {
        "gpt_client_initialized": hasattr(request.app.state, "gpt"),
        # query_cart_metadata keeps working (for whichever source IS up)
        # even if only one of the two Postgres sources is reachable, so
        # readiness only requires at least one to be up, not both.
        "ccm_db_available": db_client.is_available("ccm"),
        "coic_db_available": db_client.is_available("coic"),
        "kb_available": tools.kb_available(),
    }
    all_ok = (
        components["gpt_client_initialized"]
        and (components["ccm_db_available"] or components["coic_db_available"])
        and components["kb_available"]
    )
    payload = HealthResponse(
        status="ready" if all_ok else "not_ready",
        components=components,
    )
    if not all_ok:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=payload.model_dump(),
        )
    return payload


@app.post("/api/chat", response_model=ChatResponse, tags=["chat"])
async def chat(req: ChatRequest, request: Request):
    """
    Main endpoint. The Chrome extension posts the conversation history;
    we run the agent loop against Azure OpenAI and return the final
    answer plus a debug trace of every tool the agent called.
    """
    gpt = request.app.state.gpt

    # Convert the incoming messages into OpenAI's format. Key differences
    # from Claude:
    #   - The system prompt goes into the messages list, not as a separate
    #     `system` parameter.
    #   - The extension currently sends `content` as either a plain string
    #     (user/assistant text) or a structured list (only on assistant
    #     turns that include tool_use, which the extension does NOT
    #     round-trip). We coerce to string.
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in req.messages:
        content = m.content
        if isinstance(content, list):
            # Should not happen with the current extension, but be defensive.
            # Concatenate any text blocks; drop the rest.
            content = "".join(
                b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
            )
        messages.append({"role": m.role, "content": content})

    tool_trace: list[ToolTraceEntry] = []
    iteration = 0
    request_started = time.time()

    while iteration < MAX_TOOL_ITERATIONS:
        iteration += 1

        try:
            response = await gpt.chat.completions.create(
                model=DEPLOYMENT,               # deployment name, NOT model
                messages=messages,
                tools=tools.TOOL_SCHEMAS_OPENAI,
                max_completion_tokens=MAX_TOKENS,
                # tool_choice="auto" lets GPT decide when to call tools vs
                # answer directly. Same behavior as Claude's default.
                tool_choice="auto",
            )
        except Exception as exc:
            log.exception("Azure OpenAI call failed on iteration %d", iteration)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Upstream model error: {exc}",
            )

        choice = response.choices[0]
        finish_reason = choice.finish_reason
        assistant_message = choice.message

        if finish_reason == "tool_calls" and assistant_message.tool_calls:
            # Echo the assistant's tool-call turn back into the conversation.
            # OpenAI needs this exact shape (with tool_calls attached) so
            # subsequent tool-result messages can reference tool_call_id.
            messages.append({
                "role": "assistant",
                "content": assistant_message.content,  # usually None here
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in assistant_message.tool_calls
                ],
            })

            # Execute all tool calls from this turn CONCURRENTLY rather than
            # one at a time. When the model asks a multi-source question
            # (e.g. live New Relic status + Confluence milestone docs, or
            # cart metadata + logs), it typically emits several tool calls
            # in the same turn — running them in parallel with asyncio
            # cuts the wall-clock time down to the slowest single call
            # instead of the sum of all of them. Each tool's own I/O
            # (httpx, asyncpg pools, psycopg2 pool) is independent per call, so
            # this is safe; nothing here mutates shared state across tools.
            async def _run_one(tc):
                t0 = time.time()
                raw_args = tc.function.arguments or "{}"
                try:
                    tool_input = json.loads(raw_args)
                except json.JSONDecodeError as exc:
                    tool_input = {}
                    result = {
                        "error": (
                            f"Tool arguments were not valid JSON: {exc}. "
                            f"Try again with valid JSON matching the tool's "
                            f"parameters schema."
                        )
                    }
                else:
                    try:
                        result = await tools.dispatch_async(tc.function.name, tool_input)
                    except Exception as exc:
                        log.exception("Tool %s raised", tc.function.name)
                        result = {"error": f"Tool execution failed: {exc}"}

                duration_ms = int((time.time() - t0) * 1000)
                return tc, tool_input, result, duration_ms

            call_results = await asyncio.gather(
                *(_run_one(tc) for tc in assistant_message.tool_calls)
            )

            # Append results in the SAME order the model emitted the calls
            # (asyncio.gather preserves input order regardless of which
            # finished first), so tool_call_id pairing stays correct.
            for tc, tool_input, result, duration_ms in call_results:
                # Append the tool result — OpenAI wants one message per
                # tool call with role="tool" and the id linking it back.
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result),
                })

                tool_trace.append(ToolTraceEntry(
                    tool=tc.function.name,
                    input=tool_input,
                    result_summary=_summarize_result(tc.function.name, result),
                    duration_ms=duration_ms,
                ))

            # Loop — GPT will see the tool_result messages on the next call
            continue

        # finish_reason is one of: "stop", "length", "content_filter", ...
        # In all non-tool cases we return whatever text GPT produced.
        answer_text = assistant_message.content or ""

        if finish_reason == "length":
            answer_text += (
                "\n\n[note: response was cut off by the max_completion_tokens limit; "
                "try asking for a narrower slice]"
            )

        log.info(
            "chat completed: iters=%d tools=%d duration_ms=%d",
            iteration, len(tool_trace), int((time.time() - request_started) * 1000),
        )
        return ChatResponse(
            answer=answer_text,
            tool_trace=tool_trace,
            iterations=iteration,
        )

    log.warning("chat hit MAX_TOOL_ITERATIONS=%d", MAX_TOOL_ITERATIONS)
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"Agent loop exceeded {MAX_TOOL_ITERATIONS} iterations. "
               f"This usually means the question can't be resolved from "
               f"the available tools.",
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _summarize_result(tool_name: str, result: dict) -> str:
    """Brief one-line summary for the debug trace shown in the UI."""
    if "error" in result:
        return f"error: {result['error'][:80]}"
    if tool_name == "find_failed_cart_orders":
        return (
            f"{result.get('failed_cart_order_count', 0)} failed cart(s) "
            f"out of {result.get('total_failure_logs', 0)} exception log(s)"
        )
    if tool_name == "get_cart_order_payload":
        if result.get("found"):
            return f"payload found (matched {result.get('matched_count', 0)} record(s))"
        return "no matching payload found"
    if tool_name == "search_confluence_kb":
        n = result.get("result_count", 0)
        if n == 0:
            return "no Confluence chunks matched"
        top = result["results"][0]
        return f"{n} chunk(s); top: {top.get('chunk_id')} (distance {top.get('distance')})"
    if tool_name == "query_cart_metadata":
        if result.get("found"):
            return f"{result.get('count', 0)} row(s) returned"
        return "no match"
    return "ok"


def _launch_frontend() -> Optional[subprocess.Popen]:
    """
    Start `npm run dev` for the React app in FRONTEND_DIR as a child
    process, on FRONTEND_PORT. Returns the Popen handle (or None if
    launching was skipped/failed), so __main__ can terminate it cleanly
    on shutdown.
    """
    if not LAUNCH_FRONTEND:
        log.info("[frontend] LAUNCH_FRONTEND=false — skipping (backend-only mode).")
        return None

    if not FRONTEND_DIR.is_dir():
        log.warning(
            "[frontend] FRONTEND_DIR (%s) not found — skipping frontend "
            "launch. Set FRONTEND_DIR to the React project path, or start "
            "it yourself with `npm run dev` in that folder.",
            FRONTEND_DIR,
        )
        return None

    if not (FRONTEND_DIR / "node_modules").exists():
        log.warning(
            "[frontend] %s has no node_modules — run `npm install` there "
            "first. Skipping automatic frontend launch for now.",
            FRONTEND_DIR,
        )
        return None

    # npm is a .cmd shim on Windows; shell=True lets it resolve either way.
    npm_cmd = "npm run dev -- --port {port} --strictPort".format(port=FRONTEND_PORT)
    log.info("[frontend] Launching React dev server: %s (cwd=%s)", npm_cmd, FRONTEND_DIR)
    try:
        proc = subprocess.Popen(
            npm_cmd,
            cwd=str(FRONTEND_DIR),
            shell=True,
        )
    except Exception:
        log.exception("[frontend] Failed to launch `npm run dev`")
        return None

    log.info("[frontend] React app starting at http://localhost:%d", FRONTEND_PORT)
    return proc


if __name__ == "__main__":
    import uvicorn

    frontend_proc = _launch_frontend()
    try:
        # reload=False: the reloader spawns a second process, which would
        # launch a second copy of the frontend too. Restart `python app.py`
        # to pick up backend code changes during development instead.
        uvicorn.run("app:app", host="0.0.0.0", port=BACKEND_PORT, reload=False)
    finally:
        if frontend_proc is not None and frontend_proc.poll() is None:
            log.info("[frontend] Stopping React dev server...")
            frontend_proc.terminate()
            try:
                frontend_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                frontend_proc.kill()
