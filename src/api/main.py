"""FastAPI backend for the SRE assistant.

Provides HTTP endpoints so the agent can be consumed by web clients.
The agent is built once at startup and shared across requests.
"""

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, field_validator
from starlette.responses import StreamingResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src.agent.agent import build_agent, invoke_agent, stream_agent
from src.agent.history import (
    ConversationMetadata,
    delete_conversation,
    get_conversation,
    list_conversations,
    migrate_history_files,
    rename_conversation,
    search_conversations,
)
from src.agent.retrieval.embeddings import CHROMA_PERSIST_DIR
from src.config import get_settings
from src.observability.metrics import (
    APP_INFO,
    COMPONENT_HEALTHY,
    REPORT_DURATION,
    REPORTS_TOTAL,
    REQUEST_DURATION,
    REQUESTS_IN_PROGRESS,
    REQUESTS_TOTAL,
)
from src.report.email import is_email_configured, send_report_email
from src.report.generator import generate_report
from src.report.scheduler import start_scheduler, stop_scheduler

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class AskRequest(BaseModel):
    """Request body for POST /ask."""

    question: str
    session_id: str | None = None
    # IANA timezone name from the user's device (e.g. the browser's
    # ``Intl.DateTimeFormat().resolvedOptions().timeZone``). Optional —
    # falls back to ``settings.user_timezone`` when omitted, which is what
    # CLI clients, scheduled reports, and direct MCP callers do.
    user_timezone: str | None = None

    @field_validator("user_timezone")
    @classmethod
    def _validate_user_timezone(cls, value: str | None) -> str | None:
        """Reject non-IANA values at the request boundary so clients fail fast."""
        if value is None or value == "":
            return None
        from src.agent.tools.clock import is_valid_timezone

        if not is_valid_timezone(value):
            raise ValueError(
                f"user_timezone={value!r} is not a valid IANA timezone. "
                "Use a continent/city name like 'Europe/Amsterdam' or 'Asia/Seoul', "
                "not an abbreviation like 'CEST' or a fixed offset."
            )
        return value


class AskResponse(BaseModel):
    """Response body for POST /ask."""

    response: str
    session_id: str


class ReportRequest(BaseModel):
    """Request body for POST /report."""

    lookback_days: int | None = None


class ReportResponse(BaseModel):
    """Response body for POST /report."""

    report: str
    emailed: bool
    timestamp: str


class ComponentHealth(BaseModel):
    """Health status of a single infrastructure component."""

    name: str
    status: str
    detail: str | None = None


class HealthResponse(BaseModel):
    """Response body for GET /health."""

    status: str
    model: str
    components: list[ComponentHealth]


class ConversationSummary(BaseModel):
    """Metadata summary of a stored conversation."""

    session_id: str
    title: str
    created_at: str
    updated_at: str
    turn_count: int
    model: str
    provider: str


class ConversationDetail(BaseModel):
    """Full conversation payload including all turns."""

    session_id: str
    title: str
    created_at: str
    updated_at: str
    turn_count: int
    model: str
    provider: str
    turns: list[dict[str, str]]


class RenameRequest(BaseModel):
    """Request body for PATCH /conversations/{id}."""

    title: str


class SearchMatch(BaseModel):
    """A single matching snippet within a conversation."""

    role: str
    snippet: str


class ConversationSearchResult(BaseModel):
    """A conversation that matched a search query."""

    session_id: str
    title: str
    created_at: str
    updated_at: str
    turn_count: int
    model: str
    provider: str
    matches: list[SearchMatch]


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the agent once at startup, tear down on shutdown."""
    settings = get_settings()
    active_model = settings.anthropic_model if settings.llm_provider == "anthropic" else settings.openai_model
    APP_INFO.info({"version": "0.1.0", "model": active_model})

    logger.info("Building SRE assistant agent...")
    try:
        agent = build_agent()
        app.state.agent = agent
        logger.info("Agent ready")
    except Exception:
        logger.exception("Failed to build agent at startup")
        raise

    # Migrate legacy history files to the unified format (idempotent)
    if settings.conversation_history_dir:
        _ = migrate_history_files(settings.conversation_history_dir)

    async with AsyncExitStack() as stack:
        # Mount MCP server — auth is handled by Cloudflare Access
        from src.api.mcp_server import build_fastmcp_server

        mcp_server = build_fastmcp_server(settings)
        mcp_app = mcp_server.http_app(path="/", stateless_http=True)
        await stack.enter_async_context(mcp_app.router.lifespan_context(mcp_app))
        app.mount("/mcp", mcp_app)
        app.state.mcp_enabled = True
        tool_count = len(await mcp_server.list_tools())
        logger.info("MCP server mounted at /mcp (%d tools)", tool_count)

        start_scheduler()
        yield
        stop_scheduler()
        logger.info("Shutting down SRE assistant")


app = FastAPI(title="HomeLab SRE Assistant", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Middleware: rewrite /mcp → /mcp/ so Starlette mount() works without a 307
# redirect (some MCP clients, including Claude Code, don't follow redirects).
# ---------------------------------------------------------------------------


class TrailingSlashForMCP:
    """Add trailing slash to ``/mcp`` requests so the mounted sub-app handles them."""

    def __init__(self, inner: ASGIApp) -> None:
        self._inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] == "/mcp":
            scope = dict(scope, path="/mcp/", raw_path=b"/mcp/")
        await self._inner(scope, receive, send)


app.add_middleware(TrailingSlashForMCP)


# ---------------------------------------------------------------------------
# SSE heartbeat wrapper
# ---------------------------------------------------------------------------

type SSEEvent = dict[str, str]

# Interval between heartbeat SSE events (seconds).  Must be shorter than
# Cloudflare's 100 s idle timeout and typical client/proxy read timeouts.
_HEARTBEAT_INTERVAL_SECONDS = 15.0


async def _with_heartbeats(
    events: AsyncIterator[SSEEvent],
    interval: float = _HEARTBEAT_INTERVAL_SECONDS,
) -> AsyncIterator[SSEEvent]:
    """Wrap an async event stream with periodic heartbeat events.

    During long tool executions the agent may not yield any events for 30-60+
    seconds.  Without heartbeats the Cloudflare tunnel (100 s idle) or HTTP
    proxies in front of the client will close the connection.

    Heartbeat events have ``{"type": "heartbeat", "content": ""}``.  Clients
    that don't recognize this type should silently ignore them.
    """
    queue: asyncio.Queue[SSEEvent | None] = asyncio.Queue()
    source_error: BaseException | None = None

    async def _producer() -> None:
        nonlocal source_error
        try:
            async for event in events:
                await queue.put(event)
        except BaseException as exc:
            source_error = exc
        finally:
            await queue.put(None)  # sentinel

    async def _heartbeat() -> None:
        while True:
            await asyncio.sleep(interval)
            await queue.put({"type": "heartbeat", "content": ""})

    producer = asyncio.create_task(_producer())
    heartbeat = asyncio.create_task(_heartbeat())

    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        if not producer.done():
            await producer

    if source_error is not None:
        raise source_error


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/metrics")
async def metrics() -> Response:
    """Expose Prometheus metrics in exposition format."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/ask", response_model=AskResponse)
async def ask(request: AskRequest) -> AskResponse:
    """Send a question to the SRE assistant and get a response."""
    session_id = request.session_id or uuid4().hex[:8]
    settings = get_settings()
    REQUESTS_IN_PROGRESS.labels(endpoint="/ask").inc()
    start = time.monotonic()

    from src.agent.tools.clock import request_user_timezone

    try:
        with request_user_timezone(request.user_timezone):
            coro = invoke_agent(
                app.state.agent,
                request.question,
                session_id=session_id,
            )
            timeout = settings.request_timeout_seconds
            if timeout > 0:
                response = await asyncio.wait_for(coro, timeout=timeout)
            else:
                response = await coro
    except TimeoutError:
        duration = time.monotonic() - start
        REQUESTS_TOTAL.labels(endpoint="/ask", status="error").inc()
        REQUEST_DURATION.labels(endpoint="/ask").observe(duration)
        logger.warning("Request timed out after %.1fs", duration)
        raise HTTPException(
            status_code=504,
            detail=f"Request timed out after {duration:.0f}s",
        ) from None
    except Exception as exc:
        REQUESTS_TOTAL.labels(endpoint="/ask", status="error").inc()
        REQUEST_DURATION.labels(endpoint="/ask").observe(time.monotonic() - start)
        logger.exception("Agent invocation failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        REQUESTS_IN_PROGRESS.labels(endpoint="/ask").dec()

    duration = time.monotonic() - start
    REQUEST_DURATION.labels(endpoint="/ask").observe(duration)
    REQUESTS_TOTAL.labels(endpoint="/ask", status="success").inc()

    return AskResponse(response=response, session_id=session_id)


@app.post("/ask/stream")
async def ask_stream(request: AskRequest) -> StreamingResponse:
    """Stream agent progress as Server-Sent Events.

    Events are JSON objects with ``type`` (status/tool_start/tool_end/answer/error)
    and ``content`` fields, sent in SSE ``data:`` format.
    """
    session_id = request.session_id or uuid4().hex[:8]
    REQUESTS_IN_PROGRESS.labels(endpoint="/ask").inc()
    start = time.monotonic()

    from src.agent.tools.clock import request_user_timezone

    async def event_generator() -> AsyncIterator[str]:
        try:
            with request_user_timezone(request.user_timezone):
                raw_events = stream_agent(
                    app.state.agent,
                    request.question,
                    session_id=session_id,
                )
                async for event in _with_heartbeats(raw_events):
                    yield f"data: {json.dumps(event)}\n\n"

                    if event.get("type") == "answer":
                        REQUESTS_TOTAL.labels(endpoint="/ask", status="success").inc()
                    elif event.get("type") == "error":
                        REQUESTS_TOTAL.labels(endpoint="/ask", status="error").inc()
        except Exception as exc:
            logger.exception("Streaming agent invocation failed")
            yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
            REQUESTS_TOTAL.labels(endpoint="/ask", status="error").inc()
        finally:
            duration = time.monotonic() - start
            REQUEST_DURATION.labels(endpoint="/ask").observe(duration)
            REQUESTS_IN_PROGRESS.labels(endpoint="/ask").dec()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Check health of the assistant and its dependencies."""
    settings = get_settings()
    components: list[ComponentHealth] = []

    # --- Prometheus ---
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.prometheus_url}/-/healthy")
            if resp.status_code == 200:
                components.append(ComponentHealth(name="prometheus", status="healthy"))
            else:
                components.append(
                    ComponentHealth(
                        name="prometheus",
                        status="unhealthy",
                        detail=f"HTTP {resp.status_code}",
                    )
                )
    except Exception as exc:
        components.append(ComponentHealth(name="prometheus", status="unhealthy", detail=str(exc)))

    # --- Grafana ---
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{settings.grafana_url}/api/health",
                headers={"Authorization": f"Bearer {settings.grafana_service_account_token}"},
            )
            if resp.status_code == 200:
                components.append(ComponentHealth(name="grafana", status="healthy"))
            else:
                components.append(
                    ComponentHealth(
                        name="grafana",
                        status="unhealthy",
                        detail=f"HTTP {resp.status_code}",
                    )
                )
    except Exception as exc:
        components.append(ComponentHealth(name="grafana", status="unhealthy", detail=str(exc)))

    # --- Loki (optional) ---
    if settings.loki_url:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{settings.loki_url}/ready")
                if resp.status_code == 200:
                    components.append(ComponentHealth(name="loki", status="healthy"))
                else:
                    components.append(
                        ComponentHealth(
                            name="loki",
                            status="unhealthy",
                            detail=f"HTTP {resp.status_code}",
                        )
                    )
        except Exception as exc:
            components.append(ComponentHealth(name="loki", status="unhealthy", detail=str(exc)))

    # --- TrueNAS SCALE (optional) ---
    if settings.truenas_url:
        try:
            verify: bool = settings.truenas_verify_ssl
            async with httpx.AsyncClient(timeout=5.0, verify=verify) as client:
                resp = await client.get(
                    f"{settings.truenas_url}/api/v2.0/core/ping",
                    headers={"Authorization": f"Bearer {settings.truenas_api_key}"},
                )
                if resp.status_code == 200:
                    components.append(ComponentHealth(name="truenas", status="healthy"))
                else:
                    components.append(
                        ComponentHealth(
                            name="truenas",
                            status="unhealthy",
                            detail=f"HTTP {resp.status_code}",
                        )
                    )
        except Exception as exc:
            components.append(ComponentHealth(name="truenas", status="unhealthy", detail=str(exc)))

    # --- Proxmox VE (optional) ---
    if settings.proxmox_url:
        try:
            async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
                resp = await client.get(
                    f"{settings.proxmox_url}/api2/json/version",
                    headers={"Authorization": f"PVEAPIToken={settings.proxmox_api_token}"},
                )
                if resp.status_code == 200:
                    components.append(ComponentHealth(name="proxmox", status="healthy"))
                else:
                    components.append(
                        ComponentHealth(
                            name="proxmox",
                            status="unhealthy",
                            detail=f"HTTP {resp.status_code}",
                        )
                    )
        except Exception as exc:
            components.append(ComponentHealth(name="proxmox", status="unhealthy", detail=str(exc)))

    # --- Proxmox Backup Server (optional) ---
    if settings.pbs_url:
        try:
            async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
                resp = await client.get(
                    f"{settings.pbs_url}/api2/json/version",
                    headers={"Authorization": f"PBSAPIToken={settings.pbs_api_token}"},
                )
                if resp.status_code == 200:
                    components.append(ComponentHealth(name="pbs", status="healthy"))
                else:
                    components.append(
                        ComponentHealth(
                            name="pbs",
                            status="unhealthy",
                            detail=f"HTTP {resp.status_code}",
                        )
                    )
        except Exception as exc:
            components.append(ComponentHealth(name="pbs", status="unhealthy", detail=str(exc)))

    # --- OAuth token (SDK path only) ---
    if settings.llm_provider == "anthropic":
        try:
            from src.agent.oauth_refresh import get_token_health

            token_status, token_detail = get_token_health()
            components.append(ComponentHealth(name="oauth_token", status=token_status, detail=token_detail))
        except Exception as exc:
            components.append(ComponentHealth(name="oauth_token", status="unhealthy", detail=str(exc)))

    # --- Vector store ---
    if CHROMA_PERSIST_DIR.is_dir():
        components.append(ComponentHealth(name="vector_store", status="healthy"))
    else:
        components.append(
            ComponentHealth(
                name="vector_store",
                status="unhealthy",
                detail=f"{CHROMA_PERSIST_DIR}/ not found — run 'make ingest'",
            )
        )

    # --- MCP server (optional) ---
    if getattr(app.state, "mcp_enabled", False):
        components.append(ComponentHealth(name="mcp_server", status="healthy"))

    # --- Update Prometheus gauges ---
    for comp in components:
        COMPONENT_HEALTHY.labels(component=comp.name).set(1.0 if comp.status == "healthy" else 0.0)

    # --- Overall status ---
    healthy_count = sum(1 for c in components if c.status == "healthy")
    if healthy_count == len(components):
        overall = "healthy"
    elif healthy_count == 0:
        overall = "unhealthy"
    else:
        overall = "degraded"

    active_model = settings.anthropic_model if settings.llm_provider == "anthropic" else settings.openai_model
    return HealthResponse(status=overall, model=active_model, components=components)


# ---------------------------------------------------------------------------
# Conversation management endpoints
# ---------------------------------------------------------------------------


_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _validate_session_id(session_id: str) -> None:
    """Reject session_ids that could enable path traversal or weird paths."""
    if not _SESSION_ID_PATTERN.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id")


def _require_history_dir() -> str:
    """Return the history directory, or raise 503 if persistence is disabled."""
    settings = get_settings()
    if not settings.conversation_history_dir:
        raise HTTPException(status_code=503, detail="Conversation history is disabled")
    return settings.conversation_history_dir


@app.get("/conversations", response_model=list[ConversationSummary])
async def list_conversations_endpoint() -> list[ConversationSummary]:
    """List all stored conversations, most-recently-updated first."""
    history_dir = _require_history_dir()
    items: list[ConversationMetadata] = list_conversations(history_dir)
    return [ConversationSummary(**item) for item in items]


@app.get("/conversations/search", response_model=list[ConversationSearchResult])
async def search_conversations_endpoint(q: str = "", limit: int = 20) -> list[ConversationSearchResult]:
    """Full-text search across all conversation titles and turn content."""
    if not q.strip():
        return []
    history_dir = _require_history_dir()
    results = search_conversations(history_dir, q, max_results=min(limit, 50))
    return [
        ConversationSearchResult(
            session_id=r["session_id"],
            title=r["title"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            turn_count=r["turn_count"],
            model=r["model"],
            provider=r["provider"],
            matches=[SearchMatch(**m) for m in r["matches"]],
        )
        for r in results
    ]


@app.get("/conversations/{session_id}", response_model=ConversationDetail)
async def get_conversation_endpoint(session_id: str) -> ConversationDetail:
    """Fetch a single conversation by session_id."""
    _validate_session_id(session_id)
    history_dir = _require_history_dir()
    data = get_conversation(history_dir, session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return ConversationDetail(**data)


@app.delete("/conversations/{session_id}", status_code=204)
async def delete_conversation_endpoint(session_id: str) -> Response:
    """Delete a conversation by session_id."""
    _validate_session_id(session_id)
    history_dir = _require_history_dir()
    removed = delete_conversation(history_dir, session_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return Response(status_code=204)


@app.patch("/conversations/{session_id}", response_model=ConversationDetail)
async def rename_conversation_endpoint(session_id: str, request: RenameRequest) -> ConversationDetail:
    """Update the title of a conversation."""
    _validate_session_id(session_id)
    if not request.title.strip():
        raise HTTPException(status_code=422, detail="Title must not be empty")
    history_dir = _require_history_dir()
    updated = rename_conversation(history_dir, session_id, request.title)
    if not updated:
        raise HTTPException(status_code=404, detail="Conversation not found")
    data = get_conversation(history_dir, session_id)
    if data is None:  # pragma: no cover - raced deletion
        raise HTTPException(status_code=404, detail="Conversation not found")
    return ConversationDetail(**data)


@app.post("/report", response_model=ReportResponse)
async def report(request: ReportRequest | None = None) -> ReportResponse:
    """Generate a reliability report on demand."""
    lookback_days = request.lookback_days if request else None
    start = time.monotonic()

    try:
        result = await generate_report(lookback_days)
        emailed = False
        if is_email_configured():
            emailed = await asyncio.to_thread(send_report_email, result.markdown, result.html)

        duration = time.monotonic() - start
        REPORTS_TOTAL.labels(trigger="manual", status="success").inc()
        REPORT_DURATION.observe(duration)
        REQUESTS_TOTAL.labels(endpoint="/report", status="success").inc()
        REQUEST_DURATION.labels(endpoint="/report").observe(duration)

        return ReportResponse(
            report=result.markdown,
            emailed=emailed,
            timestamp=datetime.now(UTC).isoformat(),
        )
    except Exception as exc:
        duration = time.monotonic() - start
        REPORTS_TOTAL.labels(trigger="manual", status="error").inc()
        REPORT_DURATION.observe(duration)
        REQUESTS_TOTAL.labels(endpoint="/report", status="error").inc()
        REQUEST_DURATION.labels(endpoint="/report").observe(duration)
        logger.exception("Report generation failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
