"""FastAPI backend for the SRE assistant.

Provides HTTP endpoints so the agent can be consumed by web clients.
The agent is built once at startup and shared across requests.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from src.agent.agent import build_agent, invoke_agent, stream_agent
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

    start_scheduler()
    yield
    stop_scheduler()
    logger.info("Shutting down SRE assistant")


app = FastAPI(title="HomeLab SRE Assistant", lifespan=lifespan)


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
    REQUESTS_IN_PROGRESS.labels(endpoint="/ask").inc()
    start = time.monotonic()

    try:
        response = await invoke_agent(
            app.state.agent,
            request.question,
            session_id=session_id,
        )
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

    async def event_generator() -> AsyncIterator[str]:
        try:
            async for event in stream_agent(
                app.state.agent,
                request.question,
                session_id=session_id,
            ):
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
